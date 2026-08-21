"""Market regime — the off-switch, and the highest-leverage part of the system.

Roughly the majority of a swing strategy's drawdown comes from taking good
setups in bad tape. This module decides whether to take any at all.

DESIGN NOTE, and a deliberate departure from architecture.md v1.1:

    The verdict is computed here, in Python, from thresholds in
    config/screen.yaml. It is NOT a model output.

    The original design had a `market-regime` agent produce the RISK_ON /
    RISK_OFF label. That is the wrong shape for a component whose entire job is
    to say no. A model asked "is the tape healthy?" every morning will
    occasionally say yes on a day the rules say no, and it will say it
    persuasively -- and the one thing you cannot afford to have argued out of
    is the off-switch. So the rules decide, and the model is left with the one
    job it is actually better at: writing the sentence that explains the
    verdict to you at 5 AM.

    The LLM still sees the numbers and can add colour. It cannot flip the bit.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from core.indicators import sma
from core.screen import DEFAULT_CONFIG

RISK_ON = "RISK_ON"
NEUTRAL = "NEUTRAL"
RISK_OFF = "RISK_OFF"

TRADING_DAYS = 252


@dataclass(frozen=True)
class RegimeConfig:
    benchmark: str = "SPY"
    off_spy_below_ma: int = 50
    off_breadth_below: float = 40.0
    off_vol_above: float = 25.0
    neutral_spy_below_ma: int = 20
    neutral_breadth_below: float = 50.0
    neutral_vol_above: float = 18.0
    neutral_risk_multiplier: float = 0.5
    neutral_max_positions: int = 3

    @classmethod
    def from_yaml(cls, path: str | Path = DEFAULT_CONFIG) -> "RegimeConfig":
        raw = (yaml.safe_load(Path(path).read_text()) or {}).get("regime", {})
        off = raw.get("risk_off_if", {})
        neutral = raw.get("neutral_if", {})
        cfg = cls(
            benchmark=str(raw.get("benchmark", "SPY")),
            off_spy_below_ma=int(off.get("spy_below_ma", 50)),
            off_breadth_below=float(off.get("breadth_pct_above_200ma_below", 40)),
            off_vol_above=float(off.get("realized_vol_above", 25)),
            neutral_spy_below_ma=int(neutral.get("spy_below_ma", 20)),
            neutral_breadth_below=float(neutral.get("breadth_pct_above_200ma_below", 50)),
            neutral_vol_above=float(neutral.get("realized_vol_above", 18)),
            neutral_risk_multiplier=float(raw.get("neutral_risk_multiplier", 0.5)),
            neutral_max_positions=int(raw.get("neutral_max_positions", 3)),
        )
        cfg.validate()
        return cfg

    def validate(self) -> None:
        """The NEUTRAL band must be strictly easier to trip than RISK_OFF.

        Invert one of these by accident and the regime can never reach RISK_OFF
        -- the system would look like it had a working off-switch and would not.
        """
        if self.neutral_breadth_below < self.off_breadth_below:
            raise ValueError(
                "neutral breadth threshold is stricter than the risk-off one; "
                "RISK_OFF would be unreachable"
            )
        if self.neutral_vol_above > self.off_vol_above:
            raise ValueError(
                "neutral vol threshold is above the risk-off one; "
                "RISK_OFF would be unreachable"
            )
        if not 0 <= self.neutral_risk_multiplier <= 1:
            raise ValueError("neutral_risk_multiplier must be between 0 and 1")


@dataclass
class RegimeReading:
    verdict: str
    reasons: list[str] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    degraded: bool = False           # computed from incomplete inputs

    @property
    def tradeable(self) -> bool:
        return self.verdict != RISK_OFF

    @property
    def risk_multiplier(self) -> float:
        return {RISK_ON: 1.0, NEUTRAL: 0.5, RISK_OFF: 0.0}[self.verdict]

    def to_dict(self) -> dict:
        return {"verdict": self.verdict, "reasons": self.reasons,
                "metrics": self.metrics, "degraded": self.degraded,
                "tradeable": self.tradeable}


# --------------------------------------------------------------------------- #
# component measures
# --------------------------------------------------------------------------- #

def realized_volatility(close: pd.Series, window: int = 20) -> float:
    """Annualized close-to-close volatility, in percent.

    The free-stack stand-in for VIX. Two honest caveats: it is backward-looking
    where VIX is forward-looking, so it registers a shock a day or two late;
    and it has no volatility-risk-premium component, so its levels are not
    comparable to VIX levels. The thresholds in config are calibrated for THIS
    measure and must not be copied from VIX rules of thumb.
    """
    if len(close) < window + 1:
        return float("nan")
    returns = np.log(close / close.shift(1)).dropna().iloc[-window:]
    if len(returns) < window:
        return float("nan")
    return float(returns.std(ddof=1) * math.sqrt(TRADING_DAYS) * 100.0)


def breadth_above_ma(bars_by_symbol: dict[str, pd.DataFrame], window: int = 200) -> float:
    """Percent of the universe trading above its own N-day moving average.

    The most useful single breadth measure for this purpose: an index can be
    held up by five names while the other 495 are in downtrends, and that is
    precisely the tape where a Stage-2 screen produces its worst entries.
    """
    above = total = 0
    for bars in bars_by_symbol.values():
        close = bars.get("close")
        if close is None or len(close) < window:
            continue
        ma = sma(close, window).iloc[-1]
        last = close.iloc[-1]
        if pd.isna(ma) or pd.isna(last):
            continue
        total += 1
        above += int(last > ma)
    return (above / total * 100.0) if total else float("nan")


def benchmark_trend(close: pd.Series) -> dict:
    out: dict = {}
    for window in (20, 50, 200):
        ma = sma(close, window).iloc[-1] if len(close) >= window else float("nan")
        out[f"ma{window}"] = None if pd.isna(ma) else round(float(ma), 2)
        out[f"above_ma{window}"] = (
            None if pd.isna(ma) else bool(close.iloc[-1] > ma)
        )
    out["close"] = round(float(close.iloc[-1]), 2) if len(close) else None
    if len(close) > 5:
        out["change_5d_pct"] = round(float(close.iloc[-1] / close.iloc[-6] - 1) * 100, 2)
    return out


# --------------------------------------------------------------------------- #
# the verdict
# --------------------------------------------------------------------------- #

def assess(
    bars_by_symbol: dict[str, pd.DataFrame],
    benchmark_bars: pd.DataFrame | None = None,
    config: RegimeConfig | None = None,
    *,
    breadth: float | None = None,
) -> RegimeReading:
    """Compute the regime. Pure function of prices and config.

    Missing inputs degrade rather than crash, but a degraded reading can never
    be RISK_ON: if we cannot see the tape, we do not get to be confident about
    it. That asymmetry is the whole point -- the cost of a wrongly cautious day
    is a missed trade, and the cost of a wrongly confident one is a drawdown.
    """
    cfg = config or RegimeConfig.from_yaml()
    metrics: dict = {}
    off_reasons: list[str] = []
    neutral_reasons: list[str] = []
    degraded = False

    # -- benchmark trend ---------------------------------------------------
    bench = benchmark_bars
    if bench is None:
        bench_frame = bars_by_symbol.get(cfg.benchmark)
        bench = bench_frame if bench_frame is not None else None

    if bench is None or bench.empty:
        degraded = True
        metrics["benchmark"] = None
        neutral_reasons.append(f"{cfg.benchmark} bars unavailable")
    else:
        trend = benchmark_trend(bench["close"])
        metrics["benchmark"] = {"symbol": cfg.benchmark, **trend}
        off_key = f"above_ma{cfg.off_spy_below_ma}"
        neutral_key = f"above_ma{cfg.neutral_spy_below_ma}"
        if trend.get(off_key) is False:
            off_reasons.append(
                f"{cfg.benchmark} below its {cfg.off_spy_below_ma}-day MA")
        if trend.get(neutral_key) is False:
            neutral_reasons.append(
                f"{cfg.benchmark} below its {cfg.neutral_spy_below_ma}-day MA")

    # -- breadth -----------------------------------------------------------
    # `breadth` may be supplied precomputed. The backtest does that: it already
    # holds a date x symbol close panel, so recomputing 500 rolling 200-day
    # means for every simulated day would cost hours. Passing it in keeps the
    # VERDICT LOGIC in one place -- an earlier version of the engine reproduced
    # the thresholds locally and silently never reached RISK_ON, so every
    # backtest ran at half risk without anything looking wrong.
    if breadth is None:
        breadth = breadth_above_ma(bars_by_symbol)
    metrics["breadth_pct_above_200ma"] = None if pd.isna(breadth) else round(breadth, 1)
    if pd.isna(breadth):
        degraded = True
        neutral_reasons.append("breadth unavailable")
    else:
        if breadth < cfg.off_breadth_below:
            off_reasons.append(f"only {breadth:.0f}% of the universe above its 200-day")
        elif breadth < cfg.neutral_breadth_below:
            neutral_reasons.append(f"breadth {breadth:.0f}%, below {cfg.neutral_breadth_below:.0f}%")

    # -- volatility --------------------------------------------------------
    vol = realized_volatility(bench["close"]) if (bench is not None and not bench.empty) \
        else float("nan")
    metrics["realized_vol_pct"] = None if pd.isna(vol) else round(vol, 1)
    if pd.isna(vol):
        degraded = True
    else:
        if vol > cfg.off_vol_above:
            off_reasons.append(f"{cfg.benchmark} realized vol {vol:.0f}% "
                               f"(above {cfg.off_vol_above:.0f}%)")
        elif vol > cfg.neutral_vol_above:
            neutral_reasons.append(f"realized vol {vol:.0f}%, elevated")

    # -- verdict -----------------------------------------------------------
    if off_reasons:
        verdict, reasons = RISK_OFF, off_reasons
    elif neutral_reasons:
        verdict, reasons = NEUTRAL, neutral_reasons
    elif degraded:
        # Belt and braces: nothing tripped, but we could not see everything.
        verdict, reasons = NEUTRAL, ["regime computed from incomplete data"]
    else:
        verdict, reasons = RISK_ON, ["trend, breadth and volatility all constructive"]

    if degraded and verdict == RISK_ON:      # unreachable, but assert the invariant
        verdict, reasons = NEUTRAL, reasons + ["degraded inputs"]

    return RegimeReading(verdict=verdict, reasons=reasons, metrics=metrics,
                         degraded=degraded)


def apply_regime(reading: RegimeReading, sizing_cfg, config: RegimeConfig | None = None):
    """Return a sizing config adjusted for the regime.

    RISK_OFF never reaches here -- Stage B stops before sizing. NEUTRAL halves
    risk per trade and cuts the position count, which is the cheap version of
    "trade smaller when you are less sure".
    """
    from dataclasses import replace

    cfg = config or RegimeConfig.from_yaml()
    if reading.verdict != NEUTRAL:
        return sizing_cfg
    return replace(
        sizing_cfg,
        risk_per_trade_pct=sizing_cfg.risk_per_trade_pct * cfg.neutral_risk_multiplier,
        max_open_positions=min(sizing_cfg.max_open_positions, cfg.neutral_max_positions),
    )
