"""Rank the survivors and cap the list before any LLM sees it.

The screen answers "is this a Stage-2 uptrend?" -- a yes/no. Ranking answers
"of the ones that qualify, which are the strongest?" so that a 40-name day
still costs the same as a 12-name day. The cap is a cost control as much as a
focus control: `max_candidates` is the hard ceiling on Stage B's token spend.

Scoring is a weighted sum of percentile ranks, not of raw values. Ranking
first means a single 900%-slope outlier cannot dominate the composite, and it
makes the weights in config/screen.yaml mean what they look like they mean.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import yaml

from core.screen import DEFAULT_CONFIG, ScreenResult

# Metric -> whether a higher raw value is better.
DIRECTION = {
    "rs_rank": True,
    "distance_from_52wk_high": False,   # smaller distance is better
    "ma200_slope": True,
    "volume_trend": True,
}

# Composite metric name -> column in the metrics frame.
SOURCE_COLUMN = {
    "rs_rank": "rs_rank",
    "distance_from_52wk_high": "pct_below_52wk_high",
    "ma200_slope": "ma200_slope_pct",
    "volume_trend": "volume_trend",
}


@dataclass(frozen=True)
class RankConfig:
    weights: dict[str, float]
    max_candidates: int = 20

    @classmethod
    def from_yaml(cls, path: str | Path = DEFAULT_CONFIG) -> "RankConfig":
        raw = yaml.safe_load(Path(path).read_text()) or {}
        ranking = raw.get("ranking", {})
        weights = dict(ranking.get("weights", {}))
        unknown = set(weights) - set(SOURCE_COLUMN)
        if unknown:
            raise ValueError(
                f"unknown ranking weight(s) {sorted(unknown)}; "
                f"expected a subset of {sorted(SOURCE_COLUMN)}"
            )
        if not weights:
            raise ValueError("config/screen.yaml defines no ranking weights")
        total = sum(weights.values())
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"ranking weights must sum to 1.0, got {total:g}")
        return cls(weights=weights, max_candidates=int(ranking.get("max_candidates", 20)))


def _percentile(series: pd.Series, higher_is_better: bool) -> pd.Series:
    """Rank onto 0-1. All-equal or single-row inputs collapse to 0.5 rather
    than dividing by zero or arbitrarily crowning one row."""
    valid = series.dropna()
    if valid.empty:
        return pd.Series(0.0, index=series.index, dtype="float64")
    if len(valid) == 1 or valid.nunique() == 1:
        ranked = pd.Series(0.5, index=valid.index, dtype="float64")
    else:
        ranked = valid.rank(pct=True, method="average")
        if not higher_is_better:
            ranked = 1.0 - ranked
    # A missing metric scores 0, not 0.5: we rank on evidence, and absent
    # evidence should not be rewarded with a median score.
    return ranked.reindex(series.index).fillna(0.0)


def score(candidates: pd.DataFrame, config: RankConfig | None = None) -> pd.DataFrame:
    """Add `rank_score` plus one `pct_<metric>` column per weighted component."""
    cfg = config or RankConfig.from_yaml()
    out = candidates.copy()
    if out.empty:
        out["rank_score"] = pd.Series(dtype="float64")
        return out

    composite = pd.Series(0.0, index=out.index, dtype="float64")
    for metric, weight in cfg.weights.items():
        column = SOURCE_COLUMN[metric]
        if column not in out.columns:
            raise KeyError(f"ranking needs column {column!r}, which is not in the metrics frame")
        pct = _percentile(out[column], DIRECTION[metric])
        out[f"pct_{metric}"] = pct
        composite += weight * pct

    out["rank_score"] = composite
    return out


def top_candidates(
    result: ScreenResult, config: RankConfig | None = None
) -> pd.DataFrame:
    """Score the passing names and return at most `max_candidates`, best first.

    Ties are broken by RS rank, then alphabetically -- deterministic, so two
    runs of the same day produce the same list and the journal stays comparable.
    """
    cfg = config or RankConfig.from_yaml()
    scored = score(result.passing, cfg)
    if scored.empty:
        return scored

    # Index is the symbol. Sort by score, then RS as the tiebreak, then symbol
    # so the ordering is fully deterministic and two runs of the same day are
    # byte-comparable in the journal.
    scored = (
        scored.sort_index(ascending=True, kind="mergesort")
        .sort_values(["rank_score", "rs_rank"], ascending=[False, False], kind="mergesort")
    )

    truncated = len(scored) - cfg.max_candidates
    if truncated > 0:
        scored.attrs["truncated"] = truncated
    return scored.head(cfg.max_candidates)
