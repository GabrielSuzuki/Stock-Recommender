"""Exit rules — when to sell.

The counterpart to `screen.py`. Getting in is a screening problem; getting out
is a management problem, and it is where most of the money is actually made or
lost. Same governing rule as everything else in `core/`: **deterministic**. The
model never decides to sell.

Rules are evaluated in priority order and the first that fires wins:

    1. STOP        price is at or through the stop            -> SELL
    2. TARGET      first target reached                       -> TRIM or SELL
    3. TIME        held longer than max_hold_days             -> SELL
    4. TREND       closed below the trend MA                  -> WATCH
    5. STRENGTH    relative strength has decayed              -> WATCH

Order matters. A bar that hits both the stop and the target must be treated as
a stop, for the same reason the backtest does: intrabar sequence is unknowable
and the pessimistic reading is the safe one.

Stops only ever ratchet **up**. `update_stop` returns max(current, computed) --
a trailing rule that could lower a stop would silently widen risk on exactly
the positions that had started working.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import numpy as np
import yaml

from core.screen import DEFAULT_CONFIG

HOLD = "HOLD"
WATCH = "WATCH"
TRIM = "TRIM"
SELL = "SELL"

# Ordered by urgency, so a caller can sort or filter on severity.
SEVERITY = {HOLD: 0, WATCH: 1, TRIM: 2, SELL: 3}


@dataclass(frozen=True)
class ExitConfig:
    max_hold_days: int = 40
    breakeven_after_r: float = 1.0
    trail_after_r: float = 2.0
    trail_atr_multiple: float = 2.5
    warn_below_ma: int = 50
    warn_rs_below: float = 50.0
    scale_out_at_target: bool = True
    scale_out_fraction: float = 0.5

    @classmethod
    def from_yaml(cls, path: str | Path = DEFAULT_CONFIG) -> "ExitConfig":
        raw = (yaml.safe_load(Path(path).read_text()) or {}).get("exits", {})
        cfg = cls(
            max_hold_days=int(raw.get("max_hold_days", 40)),
            breakeven_after_r=float(raw.get("breakeven_after_r", 1.0)),
            trail_after_r=float(raw.get("trail_after_r", 2.0)),
            trail_atr_multiple=float(raw.get("trail_atr_multiple", 2.5)),
            warn_below_ma=int(raw.get("warn_below_ma", 50)),
            warn_rs_below=float(raw.get("warn_rs_below", 50.0)),
            scale_out_at_target=bool(raw.get("scale_out_at_target", True)),
            scale_out_fraction=float(raw.get("scale_out_fraction", 0.5)),
        )
        cfg.validate()
        return cfg

    def validate(self) -> None:
        if self.trail_after_r < self.breakeven_after_r:
            raise ValueError(
                "trail_after_r is below breakeven_after_r; the trailing stop "
                "would engage before breakeven and could lower the stop")
        if not 0 < self.scale_out_fraction < 1:
            raise ValueError("scale_out_fraction must be strictly between 0 and 1")
        if self.max_hold_days < 1:
            raise ValueError("max_hold_days must be at least 1")


@dataclass
class Position:
    """An open trade, as tracked rather than as planned."""
    symbol: str
    entry_date: date
    entry_price: float
    shares: float
    stop: float
    target: float
    initial_stop: float = 0.0
    sector: str = "UNKNOWN"
    thesis: str = ""
    invalidation: str = ""
    scaled_out: bool = False

    def __post_init__(self):
        if not self.initial_stop:
            self.initial_stop = self.stop

    @property
    def risk_per_share(self) -> float:
        """Always measured against the INITIAL stop.

        R must stay anchored to the risk actually taken at entry. If R were
        recomputed against a trailing stop, every ratchet would inflate the
        reported R-multiple and a mediocre trade would look like a good one.
        """
        return max(self.entry_price - self.initial_stop, 1e-9)

    def unrealised(self, price: float) -> float:
        return self.shares * (price - self.entry_price)

    def r_multiple(self, price: float) -> float:
        return (price - self.entry_price) / self.risk_per_share

    def pct_change(self, price: float) -> float:
        return (price / self.entry_price - 1.0) * 100.0

    def days_held(self, as_of: date) -> int:
        return int(np.busday_count(self.entry_date, as_of))

    def to_dict(self) -> dict:
        return {"symbol": self.symbol, "entry_date": str(self.entry_date),
                "entry_price": round(self.entry_price, 4), "shares": round(self.shares, 6),
                "stop": round(self.stop, 4), "initial_stop": round(self.initial_stop, 4),
                "target": round(self.target, 4), "sector": self.sector,
                "thesis": self.thesis, "invalidation": self.invalidation,
                "scaled_out": self.scaled_out}

    @classmethod
    def from_dict(cls, d: dict) -> "Position":
        return cls(symbol=d["symbol"], entry_date=date.fromisoformat(d["entry_date"]),
                   entry_price=float(d["entry_price"]), shares=float(d["shares"]),
                   stop=float(d["stop"]), target=float(d["target"]),
                   initial_stop=float(d.get("initial_stop") or d["stop"]),
                   sector=d.get("sector", "UNKNOWN"), thesis=d.get("thesis", ""),
                   invalidation=d.get("invalidation", ""),
                   scaled_out=bool(d.get("scaled_out", False)))


@dataclass
class ExitSignal:
    symbol: str
    action: str
    reason: str
    detail: str = ""
    suggested_stop: float | None = None
    fraction: float = 1.0            # of the remaining position
    metrics: dict = field(default_factory=dict)

    @property
    def urgent(self) -> bool:
        return self.action in (SELL, TRIM)

    def to_dict(self) -> dict:
        return {"symbol": self.symbol, "action": self.action, "reason": self.reason,
                "detail": self.detail, "suggested_stop": self.suggested_stop,
                "fraction": self.fraction, "metrics": self.metrics}


# --------------------------------------------------------------------------- #
# stop management
# --------------------------------------------------------------------------- #

def update_stop(position: Position, price: float, atr: float | None,
                config: ExitConfig) -> float:
    """The new stop for this position. Never lower than the current one.

    Two ratchets:
      - at +`breakeven_after_r`, move the stop to entry. A trade that reached
        1R and then became a loser is the single most avoidable bad outcome.
      - at +`trail_after_r`, trail by `trail_atr_multiple` ATRs, floored at
        (R - 1) locked in. Wider than the entry stop on purpose: a winner needs
        room, and tightening a trailing stop to entry-stop width is how people
        get shaken out of the trades that pay for the year.
    """
    r = position.r_multiple(price)
    candidate = position.stop

    if r >= config.breakeven_after_r:
        candidate = max(candidate, position.entry_price)

    if r >= config.trail_after_r:
        floor = position.entry_price + (r - 1.0) * position.risk_per_share
        if atr and atr > 0:
            candidate = max(candidate, min(price - config.trail_atr_multiple * atr, floor))
        else:
            candidate = max(candidate, floor)

    # Ratchet only. A rule that could lower a stop widens risk on exactly the
    # positions that had started working.
    return max(position.stop, candidate)


# --------------------------------------------------------------------------- #
# the rules
# --------------------------------------------------------------------------- #

def evaluate_position(position: Position, quote: dict, *, as_of: date,
                      config: ExitConfig | None = None) -> ExitSignal:
    """One position, one signal.

    `quote` carries today's marks: close, and optionally high, low, atr, ma50,
    rs_rank. Missing optional fields simply skip the rules that need them --
    a stale RS feed must not silently stop the stop-loss from working.
    """
    cfg = config or ExitConfig.from_yaml()
    close = float(quote["close"])
    low = float(quote.get("low", close))
    high = float(quote.get("high", close))
    atr = quote.get("atr")
    r = position.r_multiple(close)

    metrics = {"close": round(close, 4), "r_multiple": round(r, 2),
               "pct_change": round(position.pct_change(close), 2),
               "days_held": position.days_held(as_of),
               "stop": round(position.stop, 4)}

    # 1. STOP -- highest priority, and checked against the LOW, not the close.
    #    A position that traded through the stop intraday is out, whatever it
    #    closed at. Checking the close instead would report "still holding" on
    #    a name your broker already sold.
    if low <= position.stop:
        return ExitSignal(position.symbol, SELL, "stop",
                          f"traded to {low:.2f}, stop {position.stop:.2f}",
                          metrics=metrics)

    # 2. TARGET
    if high >= position.target and not position.scaled_out:
        if cfg.scale_out_at_target:
            return ExitSignal(
                position.symbol, TRIM, "target",
                f"reached {position.target:.2f} ({r:+.1f}R); "
                f"take {cfg.scale_out_fraction:.0%}, stop to breakeven",
                suggested_stop=max(position.stop, position.entry_price),
                fraction=cfg.scale_out_fraction, metrics=metrics)
        return ExitSignal(position.symbol, SELL, "target",
                          f"reached {position.target:.2f} ({r:+.1f}R)",
                          metrics=metrics)

    # 3. TIME
    held = position.days_held(as_of)
    if held >= cfg.max_hold_days:
        return ExitSignal(position.symbol, SELL, "time",
                          f"held {held} sessions ({r:+.1f}R); the thesis has "
                          "had its window", metrics=metrics)

    new_stop = update_stop(position, close, atr, cfg)
    stop_note = (None if new_stop <= position.stop + 1e-9 else round(new_stop, 2))

    # 4. TREND -- soft. Surfaced, not acted on.
    ma = quote.get(f"ma{cfg.warn_below_ma}")
    if ma and close < float(ma):
        return ExitSignal(position.symbol, WATCH, "trend_break",
                          f"closed below the {cfg.warn_below_ma}-day "
                          f"({close:.2f} vs {float(ma):.2f})",
                          suggested_stop=stop_note, metrics=metrics)

    # 5. STRENGTH -- soft.
    rs = quote.get("rs_rank")
    if rs is not None and float(rs) < cfg.warn_rs_below:
        return ExitSignal(position.symbol, WATCH, "rs_decay",
                          f"RS rank fell to {float(rs):.0f}",
                          suggested_stop=stop_note, metrics=metrics)

    return ExitSignal(position.symbol, HOLD, "on_track",
                      f"{r:+.1f}R, {held} sessions held",
                      suggested_stop=stop_note, metrics=metrics)


def evaluate_book(positions: list[Position], quotes: dict[str, dict], *,
                  as_of: date, config: ExitConfig | None = None) -> list[ExitSignal]:
    """Every open position, most urgent first.

    A position with no quote is reported as WATCH rather than skipped. Silence
    about a position you hold is the one output this must never produce.
    """
    cfg = config or ExitConfig.from_yaml()
    signals = []
    for position in positions:
        quote = quotes.get(position.symbol)
        if not quote or quote.get("close") is None:
            signals.append(ExitSignal(position.symbol, WATCH, "no_quote",
                                      "no price available — check manually"))
            continue
        signals.append(evaluate_position(position, quote, as_of=as_of, config=cfg))
    return sorted(signals, key=lambda s: (-SEVERITY[s.action], s.symbol))
