"""Indicator panels — the same math as production, computed once.

A naive backtest calls `compute_symbol_metrics` for every (symbol, date) pair:
500 x 750 = 375,000 full-history recomputations. This computes each symbol's
rolling series ONCE and stores them as date x symbol matrices, then slices a
row per date.

The critical property is that panels are built by calling **the same functions
production calls** -- `core.indicators.sma`, `.atr`, `.rs_score` and friends --
rather than by reimplementing them in vectorized form. A backtest that
reimplements the screen is testing the reimplementation. `test_backtest.py`
asserts panel rows are identical to `compute_symbol_metrics` output on random
dates, which is the test that keeps this honest.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from core import indicators as ind

# Columns `core.screen.evaluate` needs, in the shape it expects.
PANEL_FIELDS = (
    "close", "ma50", "ma150", "ma200", "ma200_prior", "ma200_slope_pct",
    "pct_below_52wk_high", "pct_above_52wk_low", "rs_score", "atr", "atr_pct",
    "avg_volume", "avg_dollar_volume", "volume_trend",
)


@dataclass
class Panels:
    """One DataFrame per metric, each indexed by date with symbols as columns."""
    frames: dict[str, pd.DataFrame]
    opens: pd.DataFrame
    highs: pd.DataFrame
    lows: pd.DataFrame
    closes: pd.DataFrame
    sectors: dict[str, str]

    @property
    def dates(self) -> pd.DatetimeIndex:
        return self.closes.index

    @property
    def symbols(self) -> list[str]:
        return list(self.closes.columns)

    def metrics_on(self, when: pd.Timestamp) -> pd.DataFrame:
        """The metrics frame for one date, shaped exactly like
        `indicators.build_metrics_frame` output so `screen.evaluate` can take it
        unchanged."""
        rows = {field: frame.loc[when] for field, frame in self.frames.items()}
        out = pd.DataFrame(rows)
        out.index.name = "symbol"

        # RS rank is cross-sectional and must be computed per date, over the
        # symbols that actually have a score on that date. Ranking against the
        # full universe including not-yet-listed names would be a subtle
        # lookahead: it would rank against companies that did not yet exist.
        out["rs_rank"] = ind.rs_rank(out["rs_score"])
        out["sector"] = [self.sectors.get(s, "UNKNOWN") for s in out.index]
        out["date"] = when
        return out.dropna(subset=["close"])


def build_panels(
    bars_by_symbol: dict[str, pd.DataFrame],
    *,
    sectors: dict[str, str] | None = None,
    atr_window: int = 14,
    volume_window: int = 50,
    ma200_lookback: int = ind.MONTH,
    min_bars: int = ind.MIN_BARS,
    on_progress=None,
) -> Panels:
    """Compute every rolling series for every symbol, once."""
    series: dict[str, dict[str, pd.Series]] = {f: {} for f in PANEL_FIELDS}
    ohlc = {"open": {}, "high": {}, "low": {}, "close": {}}

    kept = 0
    for i, (symbol, bars) in enumerate(bars_by_symbol.items()):
        if bars is None or len(bars) < min_bars:
            continue
        ind.validate_bars(bars, symbol)

        close = bars["close"]
        ma50, ma150, ma200 = ind.sma(close, 50), ind.sma(close, 150), ind.sma(close, 200)
        atr = ind.atr(bars, atr_window)

        series["close"][symbol] = close
        series["ma50"][symbol] = ma50
        series["ma150"][symbol] = ma150
        series["ma200"][symbol] = ma200
        series["ma200_prior"][symbol] = ma200.shift(ma200_lookback)
        series["ma200_slope_pct"][symbol] = ind.ma_slope_pct(ma200, ma200_lookback)
        series["pct_below_52wk_high"][symbol] = ind.pct_below_high(close)
        series["pct_above_52wk_low"][symbol] = ind.pct_above_low(close)
        series["rs_score"][symbol] = ind.rs_score(close)
        series["atr"][symbol] = atr
        series["atr_pct"][symbol] = atr / close * 100.0
        series["avg_volume"][symbol] = ind.avg_volume(bars["volume"], volume_window)
        series["avg_dollar_volume"][symbol] = ind.avg_dollar_volume(bars, volume_window)
        series["volume_trend"][symbol] = ind.volume_trend(bars["volume"])

        for column in ohlc:
            ohlc[column][symbol] = bars[column]

        kept += 1
        if on_progress and i % 50 == 0:
            on_progress(i + 1, len(bars_by_symbol))

    if not kept:
        raise ValueError("no symbol had enough history to build panels")

    frames = {field: pd.DataFrame(cols).sort_index() for field, cols in series.items()}
    return Panels(
        frames=frames,
        opens=pd.DataFrame(ohlc["open"]).sort_index(),
        highs=pd.DataFrame(ohlc["high"]).sort_index(),
        lows=pd.DataFrame(ohlc["low"]).sort_index(),
        closes=pd.DataFrame(ohlc["close"]).sort_index(),
        sectors=dict(sectors or {}),
    )
