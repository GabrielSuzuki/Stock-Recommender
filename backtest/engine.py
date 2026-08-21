"""Event-driven backtest over the panels.

Timing model, which is where most backtests quietly cheat:

    Signals are computed from the CLOSE of day D.
    Orders fill at the OPEN of day D+1.
    Exits are checked from day D+1 onward.

Nothing is ever decided using a price that had not printed yet. The one
remaining approximation is intrabar: when a bar's range contains both the stop
and the target we do not know which came first, so **the stop is assumed to
have hit**. That is pessimistic by construction, and it is the right direction
to be wrong in — an optimistic assumption there inflates the win rate on
exactly the volatile bars where it matters most.

Costs are charged on both sides. Defaults are deliberately not zero: a
frictionless backtest of a screen like this will look fine and mean nothing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from core.ranking import RankConfig, top_candidates
from core.regime import RegimeConfig, RegimeReading, assess
from core.screen import ScreenConfig, evaluate
from core.sizing import Portfolio, SizingConfig, size_candidates

log = logging.getLogger(__name__)


@dataclass
class BacktestConfig:
    start: pd.Timestamp | None = None
    end: pd.Timestamp | None = None
    starting_equity: float = 25_000.0
    max_hold_days: int = 40
    # Per side, as a fraction of notional. 0.05-0.10% is defensible for swing
    # entries in liquid names; raise it if you screen thinner stocks.
    slippage_pct: float = 0.05
    commission_per_trade: float = 0.0
    use_regime_filter: bool = True
    benchmark: str = "SPY"
    rebalance_every: int = 1          # trading days between screens

    screen: ScreenConfig = field(default_factory=ScreenConfig)
    rank: RankConfig = field(default_factory=lambda: RankConfig(
        weights={"rs_rank": 0.4, "distance_from_52wk_high": 0.2,
                 "ma200_slope": 0.2, "volume_trend": 0.2}, max_candidates=20))
    sizing: SizingConfig = field(default_factory=SizingConfig)
    regime: RegimeConfig = field(default_factory=RegimeConfig)


@dataclass
class Trade:
    symbol: str
    entry_date: pd.Timestamp
    entry_price: float
    shares: int
    stop: float
    target: float
    sector: str = "UNKNOWN"
    exit_date: pd.Timestamp | None = None
    exit_price: float | None = None
    exit_reason: str = ""
    costs: float = 0.0

    @property
    def open(self) -> bool:
        return self.exit_date is None

    @property
    def risk_dollars(self) -> float:
        return self.shares * (self.entry_price - self.stop)

    @property
    def pnl(self) -> float:
        if self.exit_price is None:
            return 0.0
        return self.shares * (self.exit_price - self.entry_price) - self.costs

    @property
    def r_multiple(self) -> float:
        risk = self.risk_dollars
        return self.pnl / risk if risk > 0 else 0.0

    @property
    def hold_days(self) -> int:
        if self.exit_date is None:
            return 0
        return int(np.busday_count(self.entry_date.date(), self.exit_date.date()))

    def to_dict(self) -> dict:
        return {"symbol": self.symbol, "sector": self.sector,
                "entry_date": str(self.entry_date.date()),
                "entry_price": round(self.entry_price, 4), "shares": self.shares,
                "stop": round(self.stop, 4), "target": round(self.target, 4),
                "exit_date": str(self.exit_date.date()) if self.exit_date else None,
                "exit_price": round(self.exit_price, 4) if self.exit_price else None,
                "exit_reason": self.exit_reason, "costs": round(self.costs, 2),
                "pnl": round(self.pnl, 2), "r_multiple": round(self.r_multiple, 3),
                "hold_days": self.hold_days}


@dataclass
class BacktestResult:
    trades: list[Trade]
    equity: pd.Series
    benchmark_equity: pd.Series | None
    regime_days: dict[str, int]
    config: BacktestConfig
    skipped_no_capacity: int = 0

    @property
    def closed(self) -> list[Trade]:
        return [t for t in self.trades if not t.open]

    def trades_frame(self) -> pd.DataFrame:
        return pd.DataFrame([t.to_dict() for t in self.trades])


# --------------------------------------------------------------------------- #

def run_backtest(panels, config: BacktestConfig | None = None,
                 on_progress=None) -> BacktestResult:
    cfg = config or BacktestConfig()
    dates = panels.dates
    if cfg.start is not None:
        dates = dates[dates >= cfg.start]
    if cfg.end is not None:
        dates = dates[dates <= cfg.end]
    if len(dates) < 2:
        raise ValueError("need at least two trading days in the window")

    equity = cfg.starting_equity
    cash = equity
    open_trades: list[Trade] = []
    all_trades: list[Trade] = []
    equity_curve: dict[pd.Timestamp, float] = {}
    regime_days: dict[str, int] = {}
    pending: list[dict] = []           # orders decided at D-1 close, filled at D open
    skipped = 0

    bench_close = panels.closes[cfg.benchmark] if cfg.benchmark in panels.closes else None

    for i, when in enumerate(dates):
        # --- 1. fill yesterday's decisions at today's open -----------------
        for order in pending:
            fill = _fill_price(panels.opens, order["symbol"], when)
            if fill is None or fill <= 0:
                continue
            cost = _entry_cost(fill, order["shares"], cfg)
            notional = fill * order["shares"] + cost
            if notional > cash:
                skipped += 1
                continue
            cash -= notional
            trade = Trade(symbol=order["symbol"], entry_date=when, entry_price=fill,
                          shares=order["shares"],
                          # Stop and target were computed from the D-1 close; scale
                          # them to the actual fill so the intended R is preserved
                          # when the stock gaps overnight.
                          stop=fill - order["risk_per_share"],
                          target=fill + order["reward_per_share"],
                          sector=order["sector"], costs=cost)
            open_trades.append(trade)
            all_trades.append(trade)
        pending = []

        # --- 2. manage open positions --------------------------------------
        still_open = []
        for trade in open_trades:
            exit_price, reason = _check_exit(panels, trade, when, cfg)
            if exit_price is None:
                still_open.append(trade)
                continue
            trade.exit_date, trade.exit_price, trade.exit_reason = when, exit_price, reason
            trade.costs += _exit_cost(exit_price, trade.shares, cfg)
            cash += exit_price * trade.shares - _exit_cost(exit_price, trade.shares, cfg)
        open_trades = still_open

        # --- 3. mark to market ---------------------------------------------
        holdings = sum(_last_price(panels.closes, t.symbol, when) * t.shares
                       for t in open_trades)
        equity = cash + holdings
        equity_curve[when] = equity

        # --- 4. decide tomorrow's orders ------------------------------------
        if i == len(dates) - 1 or i % cfg.rebalance_every:
            continue

        reading = _regime_on(panels, when, cfg) if cfg.use_regime_filter \
            else RegimeReading(verdict="RISK_ON")
        regime_days[reading.verdict] = regime_days.get(reading.verdict, 0) + 1
        if not reading.tradeable:
            continue

        pending = _decide(panels, when, open_trades, equity, reading, cfg)

        if on_progress and i % 100 == 0:
            on_progress(i + 1, len(dates))

    curve = pd.Series(equity_curve).sort_index()
    bench = None
    if bench_close is not None:
        window = bench_close.loc[curve.index].dropna()
        if not window.empty:
            bench = cfg.starting_equity * window / window.iloc[0]

    return BacktestResult(trades=all_trades, equity=curve, benchmark_equity=bench,
                          regime_days=regime_days, config=cfg,
                          skipped_no_capacity=skipped)


# --------------------------------------------------------------------------- #

def _decide(panels, when, open_trades, equity, reading, cfg) -> list[dict]:
    """Run the production screen on this date and return orders for tomorrow."""
    from core.regime import apply_regime

    metrics = panels.metrics_on(when)
    if metrics.empty:
        return []

    result = evaluate(metrics, cfg.screen)
    ranked = top_candidates(result, cfg.rank)
    if ranked.empty:
        return []

    sizing = apply_regime(reading, cfg.sizing)
    # Size against the equity we actually have, not the starting equity --
    # otherwise position sizes never compound and the whole curve is wrong.
    from dataclasses import replace
    sizing = replace(sizing, account_equity=equity)

    book = Portfolio(
        open_risk_dollars=sum(t.risk_dollars for t in open_trades),
        positions={t.symbol: t.risk_dollars for t in open_trades},
        sectors=_sector_counts(open_trades),
    )
    sized = size_candidates(ranked, book, sizing)
    taken = sized[sized["sizable"]] if not sized.empty else sized

    orders = []
    for symbol, row in taken.iterrows():
        entry, stop = float(row["entry"]), float(row["stop"])
        orders.append({
            "symbol": symbol,
            "shares": int(row["shares"]),
            "risk_per_share": entry - stop,
            "reward_per_share": float(row["target"]) - entry,
            "sector": str(row.get("sector", "UNKNOWN")),
        })
    return orders


def _check_exit(panels, trade: Trade, when, cfg) -> tuple[float | None, str]:
    """Stop, target or time. Stop wins a tie -- see the module docstring."""
    if when <= trade.entry_date:
        return None, ""

    low = _px(panels.lows, trade.symbol, when)
    high = _px(panels.highs, trade.symbol, when)
    open_px = _px(panels.opens, trade.symbol, when)
    if low is None or high is None:
        return None, ""

    if low <= trade.stop:
        # A gap through the stop fills at the open, not at the stop price.
        # Modelling every stop as filling exactly at the stop is the single
        # most common way a backtest understates drawdown.
        fill = open_px if (open_px is not None and open_px < trade.stop) else trade.stop
        return fill, "gap_stop" if fill != trade.stop else "stop"

    if high >= trade.target:
        fill = open_px if (open_px is not None and open_px > trade.target) else trade.target
        return fill, "target"

    if np.busday_count(trade.entry_date.date(), when.date()) >= cfg.max_hold_days:
        close = _px(panels.closes, trade.symbol, when)
        return close, "time"

    return None, ""


def _regime_on(panels, when, cfg) -> RegimeReading:
    """Regime as of `when`, using only bars up to and including that date.

    Breadth is computed here from the close panel (cheap: one vectorized
    rolling mean over the slice) and handed to `core.regime.assess`, which
    owns every threshold. The engine deliberately does not re-implement any
    part of the verdict.
    """
    closes = panels.closes.loc[:when]
    if len(closes) < 200:
        return RegimeReading(verdict="NEUTRAL", reasons=["insufficient history"],
                             degraded=True)

    universe = closes.drop(columns=[cfg.benchmark], errors="ignore")
    ma200 = universe.rolling(200, min_periods=200).mean().iloc[-1]
    last = universe.iloc[-1]
    valid = ma200.notna() & last.notna()
    breadth = float((last[valid] > ma200[valid]).mean() * 100) if valid.any() else None

    if cfg.benchmark not in closes:
        return RegimeReading(verdict="NEUTRAL", reasons=["no benchmark"], degraded=True)
    bench = closes[cfg.benchmark].dropna()
    if bench.empty:
        return RegimeReading(verdict="NEUTRAL", reasons=["no benchmark"], degraded=True)

    frame = pd.DataFrame({"close": bench, "open": bench, "high": bench,
                          "low": bench, "volume": 0.0})
    return assess({}, benchmark_bars=frame, config=cfg.regime, breadth=breadth)


# --------------------------------------------------------------------------- #

def _entry_cost(price: float, shares: int, cfg) -> float:
    return price * shares * cfg.slippage_pct / 100.0 + cfg.commission_per_trade


def _exit_cost(price: float, shares: int, cfg) -> float:
    return price * shares * cfg.slippage_pct / 100.0 + cfg.commission_per_trade


def _px(frame: pd.DataFrame, symbol: str, when) -> float | None:
    if symbol not in frame.columns:
        return None
    try:
        value = frame.at[when, symbol]
    except KeyError:
        return None
    return None if pd.isna(value) else float(value)


def _fill_price(opens, symbol, when) -> float | None:
    return _px(opens, symbol, when)


def _last_price(closes, symbol, when) -> float:
    value = _px(closes, symbol, when)
    if value is not None:
        return value
    series = closes[symbol].loc[:when].dropna() if symbol in closes else pd.Series(dtype=float)
    return float(series.iloc[-1]) if not series.empty else 0.0


def _sector_counts(trades: list[Trade]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for trade in trades:
        if trade.sector and trade.sector != "UNKNOWN":
            counts[trade.sector] = counts.get(trade.sector, 0) + 1
    return counts
