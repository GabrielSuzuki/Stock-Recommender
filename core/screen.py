"""Minervini Trend Template — strict 8-of-8, plus a liquidity gate.

Locked decision #2 in architecture.md: all eight conditions are combined with
AND. There is no scored variant and no "6 of 8" fallback. An empty result on a
choppy day is the screen working, not the screen failing.

The one concession to reality is that this module explains itself. Every
condition is evaluated for every symbol and kept as a column, so you can
always answer "why did nothing pass today?" -- see `funnel()`. A screen you
cannot interrogate is one you will quietly stop trusting.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import pandas as pd
import yaml

DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "config" / "screen.yaml"


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class ScreenConfig:
    """Thresholds, loaded from config/screen.yaml. Never hard-code these."""
    min_price: float = 5.00
    min_avg_volume: float = 400_000
    min_avg_dollar_volume: float = 0.0        # 0 disables; see note in from_yaml
    max_pct_below_52wk_high: float = 25.0
    min_rs_rank: float = 70.0
    ma200_lookback: int = 21
    max_candidates: int = 20

    @classmethod
    def from_yaml(cls, path: str | Path = DEFAULT_CONFIG) -> "ScreenConfig":
        raw = yaml.safe_load(Path(path).read_text()) or {}
        universe = raw.get("universe", {})
        tt = raw.get("trend_template", {})
        ranking = raw.get("ranking", {})
        return cls(
            min_price=float(universe.get("min_price", 5.00)),
            min_avg_volume=float(universe.get("min_avg_volume", 400_000)),
            min_avg_dollar_volume=float(universe.get("min_avg_dollar_volume", 0.0)),
            max_pct_below_52wk_high=float(tt.get("max_pct_below_52wk_high", 25.0)),
            min_rs_rank=float(tt.get("min_rs_rank", 70.0)),
            ma200_lookback=int(tt.get("ma200_rising_days", 21)),
            max_candidates=int(ranking.get("max_candidates", 20)),
        )


# --------------------------------------------------------------------------- #
# conditions
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Condition:
    key: str
    label: str
    test: Callable[[pd.DataFrame, ScreenConfig], pd.Series]
    gate: bool = False      # gates run before the template and are not "the screen"


def _conditions() -> list[Condition]:
    """The eight template conditions, in Minervini's order, plus two gates.

    Order matters only for the funnel readout -- the AND is order-independent.
    Gates come first so that "failed liquidity" never reads as "failed the
    trend template", which would make the funnel misleading.
    """
    return [
        # ---- gates -------------------------------------------------------- #
        Condition("price_floor", "price >= min_price",
                  lambda d, c: d["close"] >= c.min_price, gate=True),
        Condition("liquidity", "avg volume >= floor",
                  lambda d, c: (d["avg_volume"] >= c.min_avg_volume)
                  & (d["avg_dollar_volume"] >= c.min_avg_dollar_volume), gate=True),

        # ---- Trend Template, conditions 1-8 ------------------------------- #
        Condition("c1_above_ma50",   "close > 50-day MA",
                  lambda d, c: d["close"] > d["ma50"]),
        Condition("c2_above_ma150",  "close > 150-day MA",
                  lambda d, c: d["close"] > d["ma150"]),
        Condition("c3_above_ma200",  "close > 200-day MA",
                  lambda d, c: d["close"] > d["ma200"]),
        Condition("c4_ma50_gt_ma150", "50-day > 150-day",
                  lambda d, c: d["ma50"] > d["ma150"]),
        Condition("c5_ma150_gt_ma200", "150-day > 200-day",
                  lambda d, c: d["ma150"] > d["ma200"]),
        Condition("c6_ma200_rising", "200-day rising for 1 month",
                  lambda d, c: d["ma200"] > d["ma200_prior"]),
        Condition("c7_near_52wk_high", "within 25% of 52-week high",
                  lambda d, c: d["pct_below_52wk_high"] <= c.max_pct_below_52wk_high),
        Condition("c8_rs_rank",      "RS rank > 70",
                  lambda d, c: d["rs_rank"] > c.min_rs_rank),
    ]


CONDITIONS = _conditions()
GATE_KEYS = [c.key for c in CONDITIONS if c.gate]
TEMPLATE_KEYS = [c.key for c in CONDITIONS if not c.gate]


# --------------------------------------------------------------------------- #
# evaluation
# --------------------------------------------------------------------------- #

@dataclass
class ScreenResult:
    """Everything the screen knows, not just the winners."""
    evaluated: pd.DataFrame              # every symbol, every condition column
    config: ScreenConfig
    skipped_symbols: list[str] = field(default_factory=list)

    @property
    def passing(self) -> pd.DataFrame:
        """Symbols that cleared all gates and all eight conditions."""
        return self.evaluated[self.evaluated["passes"]]

    def funnel(self) -> pd.DataFrame:
        """What each condition eliminated, with two different counts.

        `eliminated` is order-dependent: names this condition rejected that
        had survived everything above it. `sole` is order-independent: names
        that ONLY this condition rejected, i.e. what would be let through if
        the condition were removed.

        The distinction matters because conditions 2 and 3 are logically
        implied by others (c1 AND c4 imply c2; c2 AND c5 imply c3), yet they
        still show a non-zero `eliminated` count on real data -- they are
        tested before c4 and c5 in Minervini's ordering, so they catch names
        those would have caught. Their `sole` count is always zero, which is
        the number that reflects the redundancy.

        This is the diagnostic for an empty candidate list. If c6/c7/c8 did the
        killing, the market genuinely has no Stage-2 leadership. If c1 killed
        everything, or the survivor count never drops, something upstream is
        broken -- stale cache, unadjusted prices, a provider returning garbage.
        """
        columns = {c.key: self.evaluated[c.key].fillna(False) for c in CONDITIONS}
        rows, alive = [], pd.Series(True, index=self.evaluated.index)
        for cond in CONDITIONS:
            col = columns[cond.key]
            newly_failed = int((alive & ~col).sum())
            alive = alive & col

            # Names every OTHER condition passes, and only this one fails.
            others = pd.Series(True, index=self.evaluated.index)
            for other in CONDITIONS:
                if other.key != cond.key:
                    others &= columns[other.key]
            rows.append({
                "condition": cond.key,
                "label": cond.label,
                "eliminated": newly_failed,
                "sole": int((others & ~col).sum()),
                "surviving": int(alive.sum()),
            })
        return pd.DataFrame(rows)

    def rejection_reasons(self) -> pd.Series:
        """First condition each non-passing symbol failed. Useful for spot checks."""
        failed = self.evaluated[~self.evaluated["passes"]]
        if failed.empty:
            return pd.Series(dtype="object")
        reasons = {}
        for symbol, row in failed.iterrows():
            for cond in CONDITIONS:
                if not bool(row[cond.key]):
                    reasons[symbol] = cond.key
                    break
            else:
                reasons[symbol] = "unknown"
        return pd.Series(reasons, name="first_failed")

    def summary(self) -> dict:
        return {
            # Fall back to the newest bar date. `as_of` is only in attrs when
            # the caller asked for a historical replay, so a live run reported
            # the literal string "None", which looks like a bug in the data.
            "as_of": _as_of_label(self.evaluated),
            "universe_size": len(self.evaluated) + len(self.skipped_symbols),
            "insufficient_history": len(self.skipped_symbols),
            "evaluated": len(self.evaluated),
            "passed_gates": int(self.evaluated["passes_gates"].sum()),
            "passed_template": int(self.evaluated["passes"].sum()),
        }


def _as_of_label(frame: pd.DataFrame) -> str:
    explicit = frame.attrs.get("as_of")
    if explicit is not None:
        return str(explicit)[:10]
    if "date" in frame.columns and not frame.empty:
        try:
            return str(frame["date"].max())[:10]
        except (TypeError, ValueError):
            pass
    return ""


def evaluate(metrics: pd.DataFrame, config: ScreenConfig | None = None) -> ScreenResult:
    """Apply the gates and the eight conditions to a metrics frame.

    `metrics` comes from `indicators.build_metrics_frame`. Every condition
    becomes a boolean column; NaN inputs produce False, never True -- an
    unknown must never be read as a pass.
    """
    cfg = config or ScreenConfig.from_yaml()
    out = metrics.copy()

    if out.empty:
        for cond in CONDITIONS:
            out[cond.key] = pd.Series(dtype="bool")
        out["passes_gates"] = pd.Series(dtype="bool")
        out["passes"] = pd.Series(dtype="bool")
        out["conditions_met"] = pd.Series(dtype="int64")
        return ScreenResult(out, cfg, list(metrics.attrs.get("skipped_symbols", [])))

    for cond in CONDITIONS:
        # Comparisons against NaN yield False in pandas, which is what we want,
        # but fillna(False) makes that guarantee explicit rather than incidental.
        out[cond.key] = cond.test(out, cfg).fillna(False).astype(bool)

    out["passes_gates"] = out[GATE_KEYS].all(axis=1)
    out["conditions_met"] = out[TEMPLATE_KEYS].sum(axis=1).astype(int)
    out["passes"] = out["passes_gates"] & out[TEMPLATE_KEYS].all(axis=1)
    out.attrs = dict(metrics.attrs)

    return ScreenResult(out, cfg, list(metrics.attrs.get("skipped_symbols", [])))


def run_screen(
    bars_by_symbol: dict[str, pd.DataFrame],
    *,
    as_of: pd.Timestamp | None = None,
    config: ScreenConfig | None = None,
) -> ScreenResult:
    """Convenience wrapper: bars in, ScreenResult out."""
    from core.indicators import build_metrics_frame

    cfg = config or ScreenConfig.from_yaml()
    metrics = build_metrics_frame(
        bars_by_symbol, as_of=as_of, ma200_lookback=cfg.ma200_lookback
    )
    return evaluate(metrics, cfg)
