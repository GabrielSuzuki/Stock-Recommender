"""Synthetic price histories with known properties.

Used by the tests and the demo. Real market data needs API keys and a network;
these let the screen be exercised deterministically, and let a test assert
"this shape MUST pass" and "this shape MUST NOT", which real data can never do.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def trading_days(n: int, end: str = "2026-08-19") -> pd.DatetimeIndex:
    return pd.bdate_range(end=pd.Timestamp(end), periods=n)


def make_bars(
    closes: np.ndarray | list[float],
    *,
    volume: float | np.ndarray = 1_000_000,
    end: str = "2026-08-19",
    intraday_range: float = 0.02,
) -> pd.DataFrame:
    """Wrap a close series into a plausible OHLCV frame.

    Highs and lows are deterministic offsets from the close, so ATR is a
    predictable function of price -- tests can assert exact values.
    """
    closes = np.asarray(closes, dtype="float64")
    idx = trading_days(len(closes), end=end)
    half = intraday_range / 2.0
    vol = np.full(len(closes), volume, dtype="float64") if np.isscalar(volume) \
        else np.asarray(volume, dtype="float64")
    return pd.DataFrame(
        {
            "open": closes * (1.0 - half / 2),
            "high": closes * (1.0 + half),
            "low": closes * (1.0 - half),
            "close": closes,
            "volume": vol,
        },
        index=idx,
    )


def stage2(n: int = 400, start: float = 50.0, annual_growth: float = 0.60,
           noise: float = 0.0, seed: int = 0, **kw) -> pd.DataFrame:
    """A clean Stage-2 uptrend: steady compounding, ends at its high.

    Passes all eight conditions by construction.
    """
    rng = np.random.default_rng(seed)
    daily = (1.0 + annual_growth) ** (1.0 / 252.0)
    closes = start * daily ** np.arange(n)
    if noise:
        closes = closes * (1.0 + rng.normal(0, noise, n))
    return make_bars(closes, **kw)


def downtrend(n: int = 400, start: float = 120.0, annual_decline: float = -0.40,
              **kw) -> pd.DataFrame:
    """Stage-4 decline. Must fail c1-c6."""
    daily = (1.0 + annual_decline) ** (1.0 / 252.0)
    return make_bars(start * daily ** np.arange(n), **kw)


def flat(n: int = 400, price: float = 40.0, **kw) -> pd.DataFrame:
    """Dead sideways. MAs converge, so c4/c5/c6 fail."""
    return make_bars(np.full(n, price), **kw)


def broken_leader(n: int = 400, start: float = 30.0, **kw) -> pd.DataFrame:
    """Rose hard for a year, then fell 40% off the high.

    The interesting negative case: MAs may still be stacked and RS may still
    be strong, but c7 (within 25% of the 52-week high) must reject it. This is
    exactly the name a naive momentum screen buys and a Trend Template does not.
    """
    up = int(n * 0.75)
    daily = (1.0 + 1.20) ** (1.0 / 252.0)
    rising = start * daily ** np.arange(up)
    peak = rising[-1]
    falling = np.linspace(peak, peak * 0.60, n - up)
    return make_bars(np.concatenate([rising, falling]), **kw)


def spiked_then_faded(n: int = 400, start: float = 20.0, growth: float = 0.70,
                      spike_at: int = 150, spike_mult: float = 2.6,
                      spike_len: int = 1, **kw) -> pd.DataFrame:
    """A steady uptrend with one isolated squeeze that sets an unrevisited high.

    This is the only shape found that fails c7 and nothing else -- the case
    condition 7 exists for. A short spike (buyout rumour, short squeeze, index
    add) barely moves a 200-day average, so the whole moving-average structure
    stays intact and RS stays strong, but the 52-week high is now ~33% above
    where the stock trades. The Trend Template rejects it, correctly: you would
    be buying a name whose recent high was a liquidity event, not a level real
    buyers defended.

    The spike must sit inside the trailing 252 bars or it rolls out of the
    window and the condition stops binding.
    """
    daily = (1.0 + growth) ** (1.0 / 252.0)
    closes = start * daily ** np.arange(n)
    closes[spike_at:spike_at + spike_len] *= spike_mult
    return make_bars(closes, **kw)


def weak_peers(count: int = 6) -> dict[str, "pd.DataFrame"]:
    """Declining names, used to give a test symbol something to out-rank.

    RS rank is cross-sectional: a one-symbol universe always scores 50 and
    therefore always fails c8. Any test that needs c8 to pass needs peers.
    """
    return {f"WEAK{i+1}": downtrend(start=60 + i * 5, annual_decline=-0.30 - 0.02 * i)
            for i in range(count)}


def recent_ipo(n: int = 150, start: float = 25.0, **kw) -> pd.DataFrame:
    """Only ~7 months of history. Must be skipped, not scored."""
    daily = (1.0 + 0.80) ** (1.0 / 252.0)
    return make_bars(start * daily ** np.arange(n), **kw)


def illiquid(n: int = 400, **kw) -> pd.DataFrame:
    """Perfect Stage-2 shape, 50k shares/day. Must fail the liquidity gate."""
    return stage2(n=n, volume=50_000, **kw)


def penny(n: int = 400, **kw) -> pd.DataFrame:
    """Perfect shape under $5. Must fail the price gate."""
    return stage2(n=n, start=0.80, **kw)


def universe(seed: int = 7) -> dict[str, pd.DataFrame]:
    """A small mixed universe covering every branch of the screen."""
    rng = np.random.default_rng(seed)
    bars: dict[str, pd.DataFrame] = {}

    # Six genuine leaders with different strengths, so ranking has work to do.
    for i, growth in enumerate([0.90, 0.75, 0.62, 0.48, 0.35, 0.28]):
        bars[f"LEAD{i+1}"] = stage2(
            annual_growth=growth, start=40 + i * 10, noise=0.004,
            seed=int(rng.integers(1e6)), volume=2_000_000 + i * 500_000,
        )

    bars["FALLER1"] = downtrend()
    bars["FALLER2"] = downtrend(start=90.0, annual_decline=-0.25)
    bars["FLAT1"] = flat()
    bars["FLAT2"] = flat(price=75.0)
    bars["BROKEN1"] = broken_leader()
    bars["BROKEN2"] = broken_leader(start=45.0)
    bars["SPIKED"] = spiked_then_faded()
    bars["NEWIPO"] = recent_ipo()
    bars["THIN"] = illiquid()
    bars["PENNY"] = penny()
    return bars


# --------------------------------------------------------------------------- #
# a synthetic market, for the backtest
# --------------------------------------------------------------------------- #

def market(
    n_symbols: int = 60,
    n_days: int = 1512,          # ~6 years
    *,
    seed: int = 42,
    bull_days: int = 380,
    bear_days: int = 90,
    market_drift: float = 0.18,
    bear_drift: float = -0.35,
    market_vol: float = 0.13,
    bear_vol: float = 0.28,
    end: str = "2026-08-19",
) -> tuple[dict[str, "pd.DataFrame"], dict[str, str]]:
    """A correlated universe with alternating bull and bear phases.

    A backtest fixture made of independent stocks is not a market. Breadth
    never moves together, so the regime filter either never fires or fires
    constantly, and the trading path is never exercised -- which is exactly
    what happened the first time this was run.

    This is a single-factor model: one market return series with regime phases,
    plus per-symbol beta and idiosyncratic noise. That is enough to produce the
    two properties the backtest needs and independent series cannot give it:

      - **breadth that swings**, so RISK_ON, NEUTRAL and RISK_OFF all occur
      - **correlation**, so the sector concentration cap has something to bite
        on and drawdowns cluster the way real ones do

    Returns (bars_by_symbol, sectors). SPY is included as the market proxy.
    """
    rng = np.random.default_rng(seed)
    idx = trading_days(n_days, end=end)

    # -- regime phases ------------------------------------------------------
    phases = np.zeros(n_days, dtype=bool)          # True = bear
    cursor = 0
    bear = False
    while cursor < n_days:
        length = bear_days if bear else bull_days
        length = int(rng.uniform(0.6, 1.4) * length)
        phases[cursor:cursor + length] = bear
        cursor += length
        bear = not bear

    daily_drift = np.where(phases, bear_drift, market_drift) / 252.0
    daily_vol = np.where(phases, bear_vol, market_vol) / np.sqrt(252.0)
    market_returns = rng.normal(daily_drift, daily_vol)

    # -- symbols ------------------------------------------------------------
    sector_names = ["Tech", "Health", "Financials", "Energy", "Industrials",
                    "Staples", "Discretionary", "Utilities"]
    bars: dict[str, pd.DataFrame] = {}
    sectors: dict[str, str] = {}

    for i in range(n_symbols):
        beta = float(rng.uniform(0.5, 1.8))
        alpha = float(rng.normal(0.04, 0.22)) / 252.0     # a few real leaders, many laggards
        idio = float(rng.uniform(0.14, 0.42)) / np.sqrt(252.0)
        returns = alpha + beta * market_returns + rng.normal(0, idio, n_days)

        start_price = float(rng.uniform(15, 260))
        closes = start_price * np.cumprod(1.0 + returns)
        closes = np.maximum(closes, 0.5)                  # no negative prices

        base_volume = float(rng.uniform(4e5, 9e6))
        # Volume rises when the stock moves, and rises more when it falls.
        move = np.abs(returns) / (idio + 1e-9)
        volume = base_volume * (0.75 + 0.5 * np.clip(move, 0, 4))

        symbol = f"SYN{i:03d}"
        bars[symbol] = make_bars(closes, volume=volume, end=end,
                                 intraday_range=float(rng.uniform(0.012, 0.045)))
        sectors[symbol] = sector_names[i % len(sector_names)]

    spy = 400.0 * np.cumprod(1.0 + market_returns)
    bars["SPY"] = make_bars(spy, volume=8e7, end=end, intraday_range=0.010)
    sectors["SPY"] = "Index"
    return bars, sectors
