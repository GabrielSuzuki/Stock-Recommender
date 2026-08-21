"""Technical indicators for the swing screen.

Every function here is a pure function of a price history and uses only bars
at or before the bar being computed. That is not a stylistic preference: a
single accidental forward-looking window turns a backtest into fiction, so
`assert_no_lookahead()` at the bottom of this module exists to be run in CI.

Conventions
-----------
Bars are a DataFrame indexed by a sorted DatetimeIndex with columns
``open, high, low, close, volume``. All functions return a Series aligned to
that index, with NaN for the warm-up period rather than a truncated series --
callers need the alignment more than they need the compactness.

Prices are assumed split- and dividend-adjusted. If they are not, every
moving average in here is wrong on the day of a split, and the screen will
fire spuriously. Alpaca's bars are adjusted; verify whatever else you plug in.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

REQUIRED_COLUMNS = ("open", "high", "low", "close", "volume")

# Trading days, used throughout. 21 ~ 1 month, 63 ~ 1 quarter, 252 ~ 1 year.
MONTH = 21
QUARTER = 63
YEAR = 252


# --------------------------------------------------------------------------- #
# validation
# --------------------------------------------------------------------------- #

def validate_bars(bars: pd.DataFrame, symbol: str = "?") -> None:
    """Raise if `bars` cannot be safely used. Fail loudly, early, and in one place."""
    missing = [c for c in REQUIRED_COLUMNS if c not in bars.columns]
    if missing:
        raise ValueError(f"{symbol}: missing column(s) {missing}")
    if not isinstance(bars.index, pd.DatetimeIndex):
        raise TypeError(f"{symbol}: index must be a DatetimeIndex, got {type(bars.index).__name__}")
    if not bars.index.is_monotonic_increasing:
        raise ValueError(f"{symbol}: bars must be sorted oldest-first")
    if bars.index.has_duplicates:
        dupes = bars.index[bars.index.duplicated()].unique()[:3].tolist()
        raise ValueError(f"{symbol}: duplicate bar dates, e.g. {dupes}")
    if (bars["high"] < bars["low"]).any():
        raise ValueError(f"{symbol}: high < low on at least one bar")
    if (bars[["open", "high", "low", "close"]] <= 0).any().any():
        raise ValueError(f"{symbol}: non-positive price found -- unadjusted or corrupt data")


# --------------------------------------------------------------------------- #
# moving averages
# --------------------------------------------------------------------------- #

def sma(series: pd.Series, window: int) -> pd.Series:
    """Simple moving average. NaN until `window` observations exist."""
    if window < 1:
        raise ValueError("window must be >= 1")
    return series.rolling(window=window, min_periods=window).mean()


def ma_rising(ma: pd.Series, lookback: int = MONTH) -> pd.Series:
    """True where the moving average is above its own value `lookback` bars ago.

    Minervini's condition 6 is "the 200-day is trending up for at least one
    month". This is the standard reading of it: compare today's MA to the MA
    one month back. It deliberately tolerates small dips inside the window --
    requiring strict monotonicity would reject almost every real uptrend.
    """
    return ma > ma.shift(lookback)


def ma_slope_pct(ma: pd.Series, lookback: int = MONTH) -> pd.Series:
    """Percent change in the moving average over `lookback` bars.

    Used for ranking, not screening: two names can both have a rising 200-day
    while one is grinding and the other is accelerating.
    """
    prior = ma.shift(lookback)
    return (ma - prior) / prior * 100.0


# --------------------------------------------------------------------------- #
# volatility
# --------------------------------------------------------------------------- #

def true_range(bars: pd.DataFrame) -> pd.Series:
    """max(H-L, |H-C_prev|, |L-C_prev|). First bar has no prior close -> H-L."""
    high, low, prev_close = bars["high"], bars["low"], bars["close"].shift(1)
    ranges = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    )
    tr = ranges.max(axis=1)
    tr.iloc[0] = (high.iloc[0] - low.iloc[0]) if len(bars) else np.nan
    return tr


def atr(bars: pd.DataFrame, window: int = 14) -> pd.Series:
    """Average True Range using Wilder's smoothing.

    Wilder's RMA, not a simple mean. The difference matters: a simple rolling
    mean drops the bar leaving the window, so ATR jumps when a single old
    outlier expires. Since ATR sets our stop distance and therefore our
    position size, that discontinuity would show up as position sizes that
    lurch for no reason related to the stock.

    RMA(n) is an EWMA with alpha = 1/n and no bias correction, seeded on the
    simple mean of the first n true ranges -- which is exactly what Wilder
    specified and what charting packages display.
    """
    if window < 1:
        raise ValueError("window must be >= 1")
    tr = true_range(bars)
    if len(tr) < window:
        return pd.Series(np.nan, index=bars.index, dtype="float64")

    out = pd.Series(np.nan, index=bars.index, dtype="float64")
    seed = tr.iloc[:window].mean()
    out.iloc[window - 1] = seed
    prev = seed
    tr_values = tr.to_numpy(dtype="float64")
    for i in range(window, len(tr)):
        prev = (prev * (window - 1) + tr_values[i]) / window
        out.iloc[i] = prev
    return out


# --------------------------------------------------------------------------- #
# position within the 52-week range
# --------------------------------------------------------------------------- #

def rolling_high(series: pd.Series, window: int = YEAR) -> pd.Series:
    """Highest close over the trailing `window` bars, inclusive of today."""
    return series.rolling(window=window, min_periods=window).max()


def rolling_low(series: pd.Series, window: int = YEAR) -> pd.Series:
    return series.rolling(window=window, min_periods=window).min()


def pct_below_high(close: pd.Series, window: int = YEAR) -> pd.Series:
    """How far below the trailing high we are, as a positive percentage.

    0 means sitting at a new high. 25 means 25% off it. Minervini's condition 7
    is "within 25% of the 52-week high", i.e. this value <= 25.
    """
    high = rolling_high(close, window)
    return (high - close) / high * 100.0


def pct_above_low(close: pd.Series, window: int = YEAR) -> pd.Series:
    """Percent above the trailing low. Not a screen condition, but useful
    context: a name 200% off its low is at a different point in its move than
    one 30% off, even if both are near their highs."""
    low = rolling_low(close, window)
    return (close - low) / low * 100.0


# --------------------------------------------------------------------------- #
# relative strength
# --------------------------------------------------------------------------- #

def rs_score(close: pd.Series) -> pd.Series:
    """IBD-style weighted trailing return, most recent quarter double-weighted.

        score = 2*Q1 + Q2 + Q3 + Q4

    where Qn is the simple return over quarter n counting back from today.
    This is the raw score; it is only meaningful once ranked cross-sectionally
    against the rest of the universe, which is what `rs_rank` does.

    Returns NaN until a full year of history exists -- a name that IPO'd eight
    months ago has no comparable score and must not be silently given one.
    """
    q1 = close / close.shift(QUARTER) - 1.0
    q2 = close.shift(QUARTER) / close.shift(2 * QUARTER) - 1.0
    q3 = close.shift(2 * QUARTER) / close.shift(3 * QUARTER) - 1.0
    q4 = close.shift(3 * QUARTER) / close.shift(4 * QUARTER) - 1.0
    return 2.0 * q1 + q2 + q3 + q4


def rs_rank(scores: pd.Series) -> pd.Series:
    """Percentile-rank raw RS scores across the universe onto 1-99.

    `scores` is one value per symbol on a single date -- this is the only
    cross-sectional function in the module. Names with NaN scores (insufficient
    history) stay NaN and are excluded by the screen rather than ranked last,
    because "unknown" and "worst" are different facts.
    """
    valid = scores.dropna()
    if valid.empty:
        return pd.Series(np.nan, index=scores.index, dtype="float64")
    if len(valid) == 1:
        ranked = pd.Series(50.0, index=valid.index)
    else:
        ranked = valid.rank(pct=True, method="average") * 98.0 + 1.0
    return ranked.reindex(scores.index)


# --------------------------------------------------------------------------- #
# liquidity
# --------------------------------------------------------------------------- #

def avg_volume(volume: pd.Series, window: int = 50) -> pd.Series:
    return volume.rolling(window=window, min_periods=window).mean()


def avg_dollar_volume(bars: pd.DataFrame, window: int = 50) -> pd.Series:
    """Average daily dollar volume -- a better liquidity gate than share count,
    because 400k shares of a $6 stock and 400k of a $400 stock are not remotely
    the same amount of tradeable liquidity."""
    dollars = bars["close"] * bars["volume"]
    return dollars.rolling(window=window, min_periods=window).mean()


def volume_trend(volume: pd.Series, fast: int = 21, slow: int = 63) -> pd.Series:
    """Ratio of recent to longer-run average volume. >1 means participation is
    picking up, which is what you want to see under a base breakout."""
    return avg_volume(volume, fast) / avg_volume(volume, slow)


# --------------------------------------------------------------------------- #
# per-symbol assembly
# --------------------------------------------------------------------------- #

MIN_BARS = 4 * QUARTER + 1          # 253: one year of RS history plus a prior bar


def compute_symbol_metrics(
    bars: pd.DataFrame,
    symbol: str,
    *,
    as_of: pd.Timestamp | None = None,
    atr_window: int = 14,
    volume_window: int = 50,
    ma200_lookback: int = MONTH,
) -> dict | None:
    """Collapse one symbol's history into a single row of metrics.

    Returns None when there is not enough history, rather than a row of NaNs:
    downstream code should never have to distinguish "failed the screen" from
    "we never had the data".

    `as_of` truncates the history *before* anything is computed. This is the
    single line that makes historical replay honest -- without it a backtest
    would be free to peek at bars that had not happened yet.
    """
    validate_bars(bars, symbol)
    if as_of is not None:
        bars = bars.loc[bars.index <= as_of]
    if len(bars) < MIN_BARS:
        return None

    close = bars["close"]
    ma50, ma150, ma200 = sma(close, 50), sma(close, 150), sma(close, 200)
    last = -1

    def val(series: pd.Series) -> float:
        v = series.iloc[last]
        return float(v) if pd.notna(v) else float("nan")

    return {
        "symbol": symbol,
        "date": bars.index[last],
        "close": val(close),
        "ma50": val(ma50),
        "ma150": val(ma150),
        "ma200": val(ma200),
        "ma200_prior": float(ma200.iloc[last - ma200_lookback])
        if len(ma200) > ma200_lookback and pd.notna(ma200.iloc[last - ma200_lookback])
        else float("nan"),
        "ma200_slope_pct": val(ma_slope_pct(ma200, ma200_lookback)),
        "pct_below_52wk_high": val(pct_below_high(close)),
        "pct_above_52wk_low": val(pct_above_low(close)),
        "rs_score": val(rs_score(close)),
        "atr": val(atr(bars, atr_window)),
        "atr_pct": val(atr(bars, atr_window) / close * 100.0),
        "avg_volume": val(avg_volume(bars["volume"], volume_window)),
        "avg_dollar_volume": val(avg_dollar_volume(bars, volume_window)),
        "volume_trend": val(volume_trend(bars["volume"])),
        "bars_available": len(bars),
    }


def build_metrics_frame(
    bars_by_symbol: dict[str, pd.DataFrame],
    *,
    as_of: pd.Timestamp | None = None,
    **kwargs,
) -> pd.DataFrame:
    """Metrics for the whole universe, one row per symbol, RS ranked.

    Symbols with insufficient history are dropped here and counted -- see
    `skipped_symbols` on the returned frame's ``.attrs``.
    """
    rows, skipped = [], []
    for symbol, bars in bars_by_symbol.items():
        row = compute_symbol_metrics(bars, symbol, as_of=as_of, **kwargs)
        (rows.append(row) if row is not None else skipped.append(symbol))

    if not rows:
        empty = pd.DataFrame(columns=["symbol", "rs_rank"]).set_index("symbol")
        empty.attrs["skipped_symbols"] = skipped
        return empty

    frame = pd.DataFrame(rows).set_index("symbol")
    frame["rs_rank"] = rs_rank(frame["rs_score"])
    frame.attrs["skipped_symbols"] = skipped
    frame.attrs["as_of"] = as_of
    return frame.sort_index()


# --------------------------------------------------------------------------- #
# lookahead guard
# --------------------------------------------------------------------------- #

def assert_no_lookahead(bars: pd.DataFrame, symbol: str = "TEST") -> None:
    """Prove that appending future bars does not change past metrics.

    Computes metrics as of a mid-history date using the full frame, then again
    using only the truncated frame, and asserts the two agree. If any indicator
    ever acquires a centred window or a backfill, this fails.

    Cheap enough to run in CI on one synthetic symbol. Do that.
    """
    if len(bars) < MIN_BARS + 20:
        raise ValueError("need more history to run the lookahead check")
    cut = bars.index[-11]
    with_future = compute_symbol_metrics(bars, symbol, as_of=cut)
    without_future = compute_symbol_metrics(bars.loc[:cut], symbol)
    if with_future is None or without_future is None:
        raise AssertionError("lookahead check could not compute metrics")

    for key, a in with_future.items():
        b = without_future[key]
        if isinstance(a, float) and isinstance(b, float):
            if not (np.isnan(a) and np.isnan(b)) and not np.isclose(a, b, rtol=1e-12, atol=1e-12):
                raise AssertionError(f"lookahead detected in {key!r}: {a} != {b}")
        elif a != b:
            raise AssertionError(f"lookahead detected in {key!r}: {a} != {b}")
