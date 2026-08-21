"""Position sizing: how many shares, where the stop goes, and when to say no.

Sizing is where a screen becomes a trade plan, and where most of the damage in
a discretionary process actually happens -- not in picking the wrong stock, but
in taking too much of the right one. Every number here is deterministic and
comes from config/screen.yaml, so the model never gets to improvise a size.

Four independent caps apply, and the binding one wins:

  1. risk       -- risk_per_trade_pct of equity, measured to the stop
  2. notional   -- max_position_pct of equity, so one name cannot dominate
  3. liquidity  -- max_participation_pct of 50-day average volume
  4. heat       -- total open risk across all positions

Cap 3 is the one people leave out. A position you cannot exit in a day is not
a position, it is a hostage situation -- and on a 400k-share liquidity floor
with a small account it will occasionally bind, which is the point.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
import yaml

from core.screen import DEFAULT_CONFIG


@dataclass(frozen=True)
class SizingConfig:
    account_equity: float = 25_000.0
    risk_per_trade_pct: float = 0.75
    atr_stop_multiple: float = 2.0
    target_r_multiple: float = 2.5
    max_open_positions: int = 6
    max_portfolio_heat_pct: float = 4.0
    max_position_pct: float = 25.0
    max_same_sector: int = 2
    max_participation_pct: float = 0.5
    min_shares: int = 1
    # Fractional shares. Off by default because whole shares are what most
    # people actually trade, but essential for a small account: at $400 equity
    # and 0.75% risk, the whole-share model returns zero shares on any stock
    # above about $20, and the account would sit in cash proving nothing.
    allow_fractional: bool = False
    min_notional: float = 1.0        # only meaningful when fractional

    @classmethod
    def from_yaml(cls, path: str | Path = DEFAULT_CONFIG) -> "SizingConfig":
        raw = (yaml.safe_load(Path(path).read_text()) or {}).get("sizing", {})
        equity = float(os.environ.get("SCREENER_EQUITY", raw.get("account_equity", 25_000)))
        if equity <= 0:
            raise ValueError("account_equity must be positive")
        cfg = cls(
            account_equity=equity,
            risk_per_trade_pct=float(raw.get("risk_per_trade_pct", 0.75)),
            atr_stop_multiple=float(raw.get("atr_stop_multiple", 2.0)),
            target_r_multiple=float(raw.get("target_r_multiple", 2.5)),
            max_open_positions=int(raw.get("max_open_positions", 6)),
            max_portfolio_heat_pct=float(raw.get("max_portfolio_heat_pct", 4.0)),
            max_position_pct=float(raw.get("max_position_pct", 25.0)),
            max_same_sector=int(raw.get("max_same_sector", 2)),
            max_participation_pct=float(raw.get("max_participation_pct", 0.5)),
            min_shares=int(raw.get("min_shares", 1)),
            allow_fractional=bool(raw.get("allow_fractional", False)),
            min_notional=float(raw.get("min_notional", 1.0)),
        )
        cfg.validate()
        return cfg

    def validate(self) -> None:
        if not 0 < self.risk_per_trade_pct <= 5:
            raise ValueError(f"risk_per_trade_pct {self.risk_per_trade_pct} outside 0-5%")
        if self.atr_stop_multiple <= 0:
            raise ValueError("atr_stop_multiple must be positive")
        if self.max_portfolio_heat_pct < self.risk_per_trade_pct:
            raise ValueError(
                "max_portfolio_heat_pct is below risk_per_trade_pct -- no trade could "
                "ever be taken"
            )
        if self.max_open_positions < 1:
            raise ValueError("max_open_positions must be >= 1")

    @property
    def risk_dollars(self) -> float:
        return self.account_equity * self.risk_per_trade_pct / 100.0


@dataclass
class Portfolio:
    """What is already open. Sizing has to know, or it double-loads a theme."""
    open_risk_dollars: float = 0.0
    positions: dict[str, float] = field(default_factory=dict)   # symbol -> risk $
    sectors: dict[str, int] = field(default_factory=dict)       # sector -> count

    @property
    def count(self) -> int:
        return len(self.positions)

    def heat_pct(self, equity: float) -> float:
        return self.open_risk_dollars / equity * 100.0 if equity else 0.0

    @classmethod
    def empty(cls) -> "Portfolio":
        return cls()


# --------------------------------------------------------------------------- #
# single-position math
# --------------------------------------------------------------------------- #

def stop_price(entry: float, atr_value: float, multiple: float) -> float:
    """Stop below entry by `multiple` ATRs.

    ATR-based rather than a fixed percentage so the stop adapts to the stock's
    own volatility: a 2% stop is loose on a utility and suicidal on a biotech.
    """
    return entry - multiple * atr_value


def target_price(entry: float, stop: float, r_multiple: float) -> float:
    return entry + (entry - stop) * r_multiple


def raw_shares(risk_dollars: float, entry: float, stop: float,
               fractional: bool = False) -> float:
    """Shares such that a stop-out loses approximately `risk_dollars`.

    Whole shares round DOWN, so realised risk lands under budget rather than
    over. Fractional shares are exact.
    """
    per_share_risk = entry - stop
    if per_share_risk <= 0:
        return 0.0
    exact = risk_dollars / per_share_risk
    return round(exact, 6) if fractional else float(math.floor(exact))


def apply_caps(
    shares: float, entry: float, avg_volume: float, cfg: SizingConfig
) -> tuple[float, str | None]:
    """Apply the notional and liquidity caps. Returns (shares, binding_cap)."""
    binding = None

    def _round(value: float) -> float:
        return round(value, 6) if cfg.allow_fractional else float(math.floor(value))

    max_notional = cfg.account_equity * cfg.max_position_pct / 100.0
    notional_cap = _round(max_notional / entry) if entry > 0 else 0.0
    if notional_cap < shares:
        shares, binding = notional_cap, "notional"

    if avg_volume and avg_volume > 0:
        participation_cap = _round(avg_volume * cfg.max_participation_pct / 100.0)
        if participation_cap < shares:
            shares, binding = participation_cap, "liquidity"

    return max(shares, 0.0), binding


# --------------------------------------------------------------------------- #
# portfolio-level assignment
# --------------------------------------------------------------------------- #

REJECTIONS = {
    "no_atr": "ATR unavailable",
    "stop_below_zero": "ATR stop would be at or below zero",
    "too_small": "size rounds below min_shares",
    "heat": "portfolio heat cap reached",
    "slots": "max_open_positions reached",
    "sector": "max_same_sector reached",
    "already_held": "already in the portfolio",
}

UNKNOWN_SECTOR = "UNKNOWN"


def size_candidates(
    candidates: pd.DataFrame,
    portfolio: Portfolio | None = None,
    config: SizingConfig | None = None,
    *,
    sector_column: str = "sector",
) -> pd.DataFrame:
    """Attach a full trade plan to each candidate, in rank order.

    Candidates are consumed best-first, so the portfolio caps allocate scarce
    capacity to the highest-ranked names rather than to whoever happens to be
    alphabetically first. Rejected candidates are kept in the frame with a
    `sizing_rejection` reason rather than dropped -- the brief should be able
    to say "we liked NVDA but the sector was full", which is more useful than
    silence.
    """
    cfg = config or SizingConfig.from_yaml()
    book = portfolio or Portfolio.empty()

    out = candidates.copy()
    for col, default in [
        ("entry", float("nan")), ("stop", float("nan")), ("target", float("nan")),
        ("shares", 0.0), ("risk_dollars", 0.0), ("position_value", 0.0),
        ("r_multiple", float("nan")), ("binding_cap", None),
        ("sizable", False), ("sizing_rejection", None), ("sector_known", False),
    ]:
        out[col] = default
    if out.empty:
        return out

    heat_budget = cfg.account_equity * cfg.max_portfolio_heat_pct / 100.0 - book.open_risk_dollars
    slots = cfg.max_open_positions - book.count
    sector_counts = dict(book.sectors)

    for symbol in out.index:
        row = out.loc[symbol]

        if symbol in book.positions:
            out.at[symbol, "sizing_rejection"] = REJECTIONS["already_held"]
            continue
        if slots <= 0:
            out.at[symbol, "sizing_rejection"] = REJECTIONS["slots"]
            continue

        sector = str(row.get(sector_column, "") or UNKNOWN_SECTOR).strip() or UNKNOWN_SECTOR
        # The concentration cap is deliberately NOT enforced when the sector is
        # unknown. Without this, a missing sector feed collapses every candidate
        # into one bucket and silently caps the whole book at max_same_sector --
        # you would take 2 positions instead of 6 and the only clue would be a
        # rejection reason that is not actually true. Refusing to constrain on a
        # value we do not have is the lesser error; `sectors_unknown` in the
        # summary makes the missing protection visible instead of silent.
        if sector != UNKNOWN_SECTOR and sector_counts.get(sector, 0) >= cfg.max_same_sector:
            out.at[symbol, "sizing_rejection"] = REJECTIONS["sector"]
            continue

        entry = float(row["close"])
        atr_value = float(row.get("atr", float("nan")))
        if not (atr_value > 0):
            out.at[symbol, "sizing_rejection"] = REJECTIONS["no_atr"]
            continue

        stop = stop_price(entry, atr_value, cfg.atr_stop_multiple)
        if stop <= 0:
            # Happens on very high-ATR, low-priced names. A stop at zero is not
            # a stop; refuse rather than sizing off a meaningless risk figure.
            out.at[symbol, "sizing_rejection"] = REJECTIONS["stop_below_zero"]
            continue

        per_share_risk = entry - stop
        shares = raw_shares(cfg.risk_dollars, entry, stop, cfg.allow_fractional)
        shares, binding = apply_caps(shares, entry, float(row.get("avg_volume", 0) or 0), cfg)

        # The heat cap trims rather than rejects: a partial position that fits
        # the remaining risk budget is better than skipping the best name
        # because it did not fit at full size.
        risk = shares * per_share_risk
        if risk > heat_budget:
            exact = heat_budget / per_share_risk
            shares = round(exact, 6) if cfg.allow_fractional else float(math.floor(exact))
            binding = "heat"
            risk = shares * per_share_risk

        floor = (cfg.min_notional / entry) if cfg.allow_fractional else cfg.min_shares
        if shares < floor:
            out.at[symbol, "sizing_rejection"] = (
                REJECTIONS["heat"] if binding == "heat" else REJECTIONS["too_small"]
            )
            continue

        out.at[symbol, "entry"] = entry
        out.at[symbol, "stop"] = stop
        out.at[symbol, "target"] = target_price(entry, stop, cfg.target_r_multiple)
        out.at[symbol, "shares"] = shares
        out.at[symbol, "risk_dollars"] = risk
        out.at[symbol, "position_value"] = shares * entry
        out.at[symbol, "r_multiple"] = cfg.target_r_multiple
        out.at[symbol, "binding_cap"] = binding
        out.at[symbol, "sizable"] = True
        out.at[symbol, "sector_known"] = sector != UNKNOWN_SECTOR

        heat_budget -= risk
        slots -= 1
        if sector != UNKNOWN_SECTOR:
            sector_counts[sector] = sector_counts.get(sector, 0) + 1

    return out


def portfolio_summary(sized: pd.DataFrame, config: SizingConfig | None = None) -> dict:
    cfg = config or SizingConfig.from_yaml()
    taken = sized[sized["sizable"]] if not sized.empty else sized
    unknown_sector = int((~taken["sector_known"].astype(bool)).sum()) if not taken.empty else 0
    total_risk = float(taken["risk_dollars"].sum()) if not taken.empty else 0.0
    total_value = float(taken["position_value"].sum()) if not taken.empty else 0.0
    return {
        "equity": cfg.account_equity,
        "risk_per_trade": round(cfg.risk_dollars, 2),
        "positions": int(len(taken)),
        "total_risk_dollars": round(total_risk, 2),
        "portfolio_heat_pct": round(total_risk / cfg.account_equity * 100.0, 2),
        "total_position_value": round(total_value, 2),
        "gross_exposure_pct": round(total_value / cfg.account_equity * 100.0, 2),
        # Non-zero means the concentration cap was not enforced for that many
        # positions. Worth surfacing in the brief, not burying here.
        "sectors_unknown": unknown_sector,
    }
