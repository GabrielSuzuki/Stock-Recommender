"""Walk-forward validation.

architecture.md §10 rule 1: never a single split. A screen tuned on 2023-2025
and tested on 2023-2025 tells you nothing at all -- it tells you that an
optimizer can fit noise, which was never in doubt.

The protocol here:

    |<-- train 2y -->|<- test 6m ->|
              |<-- train 2y -->|<- test 6m ->|
                        |<-- train 2y -->|<- test 6m ->|

Each training window picks the best parameters by a stated objective; those
parameters are then applied, untouched, to the following out-of-sample window.
**Only the concatenated out-of-sample segments are reported.** The in-sample
numbers are computed and kept, but they are diagnostic -- if they look far
better than out-of-sample, you have measured your own overfitting, which is
useful, but it is not a result.

If you run this with an empty parameter grid it degrades to a plain rolling
out-of-sample test, which is still worth far more than a single split.
"""

from __future__ import annotations

import itertools
import logging
from dataclasses import dataclass, field, replace

import pandas as pd

from backtest.engine import BacktestConfig, run_backtest
from backtest.metrics import performance

log = logging.getLogger(__name__)

TRADING_DAYS_PER_YEAR = 252


@dataclass
class Window:
    train_start: pd.Timestamp
    train_end: pd.Timestamp
    test_start: pd.Timestamp
    test_end: pd.Timestamp
    chosen: dict = field(default_factory=dict)
    train_report: dict = field(default_factory=dict)
    test_report: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "train": [str(self.train_start.date()), str(self.train_end.date())],
            "test": [str(self.test_start.date()), str(self.test_end.date())],
            "chosen": self.chosen,
            "train_expectancy_r": self.train_report.get("expectancy_r"),
            "test_expectancy_r": self.test_report.get("expectancy_r"),
            "test_return_pct": self.test_report.get("total_return_pct"),
            "test_trades": self.test_report.get("trades"),
        }


@dataclass
class WalkForwardResult:
    windows: list[Window]
    oos_equity: pd.Series
    oos_trades: list
    report: dict
    overfitting_gap: float | None = None


def make_windows(dates: pd.DatetimeIndex, *, train_days: int = 2 * TRADING_DAYS_PER_YEAR,
                 test_days: int = TRADING_DAYS_PER_YEAR // 2,
                 step_days: int | None = None) -> list[Window]:
    """Rolling train/test splits. Step defaults to the test length (no overlap)."""
    step = step_days or test_days
    windows: list[Window] = []
    start = 0
    while start + train_days + test_days <= len(dates):
        train_slice = dates[start:start + train_days]
        test_slice = dates[start + train_days:start + train_days + test_days]
        windows.append(Window(train_start=train_slice[0], train_end=train_slice[-1],
                              test_start=test_slice[0], test_end=test_slice[-1]))
        start += step
    return windows


def _grid(params: dict[str, list]) -> list[dict]:
    if not params:
        return [{}]
    keys = list(params)
    return [dict(zip(keys, values)) for values in itertools.product(*(params[k] for k in keys))]


def _apply(config: BacktestConfig, params: dict) -> BacktestConfig:
    """Overlay a parameter set onto a config.

    Keys are dotted: "screen.min_rs_rank", "sizing.atr_stop_multiple".
    """
    out = config
    for dotted, value in params.items():
        section, _, field_name = dotted.partition(".")
        if not field_name:
            out = replace(out, **{section: value})
            continue
        sub = getattr(out, section)
        out = replace(out, **{section: replace(sub, **{field_name: value})})
    return out


def walk_forward(
    panels,
    base_config: BacktestConfig | None = None,
    param_grid: dict[str, list] | None = None,
    *,
    train_days: int = 2 * TRADING_DAYS_PER_YEAR,
    test_days: int = TRADING_DAYS_PER_YEAR // 2,
    objective: str = "expectancy_r",
    min_trades: int = 10,
    on_progress=None,
) -> WalkForwardResult:
    """Run the protocol. Returns concatenated out-of-sample results only."""
    cfg = base_config or BacktestConfig()
    grid = _grid(param_grid or {})
    windows = make_windows(panels.dates, train_days=train_days, test_days=test_days)
    if not windows:
        raise ValueError(
            f"not enough history for a {train_days}+{test_days} day walk-forward; "
            f"have {len(panels.dates)} trading days")

    log.info("walk-forward: %d windows x %d parameter set(s)", len(windows), len(grid))

    oos_trades: list = []
    oos_segments: list[pd.Series] = []

    for i, window in enumerate(windows):
        best, best_score, best_report = grid[0], float("-inf"), {}
        for params in grid:
            train_cfg = _apply(replace(cfg, start=window.train_start,
                                       end=window.train_end), params)
            report = performance(run_backtest(panels, train_cfg))
            score = report.get(objective)
            # A parameter set that produced three trades has not been measured,
            # it has been sampled. Refusing to select on a tiny sample is the
            # cheapest overfitting guard available.
            if report.get("trades", 0) < min_trades or score is None or score != score:
                continue
            if score > best_score:
                best, best_score, best_report = params, score, report

        window.chosen = best
        window.train_report = best_report

        test_cfg = _apply(replace(cfg, start=window.test_start, end=window.test_end), best)
        test_result = run_backtest(panels, test_cfg)
        window.test_report = performance(test_result)

        oos_trades.extend(test_result.trades)
        oos_segments.append(test_result.equity)

        if on_progress:
            on_progress(i + 1, len(windows))

    equity = _stitch(oos_segments, cfg.starting_equity)
    report = _oos_report(equity, oos_trades, windows, cfg, panels)

    train_scores = [w.train_report.get(objective) for w in windows
                    if w.train_report.get(objective) is not None]
    test_scores = [w.test_report.get(objective) for w in windows
                   if w.test_report.get(objective) is not None]
    gap = None
    if train_scores and test_scores:
        gap = round(sum(train_scores) / len(train_scores)
                    - sum(test_scores) / len(test_scores), 4)

    return WalkForwardResult(windows=windows, oos_equity=equity,
                             oos_trades=oos_trades, report=report,
                             overfitting_gap=gap)


def _stitch(segments: list[pd.Series], starting_equity: float) -> pd.Series:
    """Chain the out-of-sample segments into one compounding curve.

    Each segment starts fresh at `starting_equity` inside its own window, so
    they are chained by return rather than concatenated by level -- otherwise
    the curve would reset to the starting value every six months and the
    reported CAGR would be meaningless.
    """
    if not segments:
        return pd.Series(dtype=float)
    pieces, level = [], starting_equity
    for segment in segments:
        segment = segment.dropna()
        if segment.empty or segment.iloc[0] <= 0:
            continue
        scaled = segment / segment.iloc[0] * level
        pieces.append(scaled)
        level = float(scaled.iloc[-1])
    if not pieces:
        return pd.Series(dtype=float)
    return pd.concat(pieces).sort_index()


def _oos_report(equity, trades, windows, cfg, panels) -> dict:
    from backtest.engine import BacktestResult

    bench = None
    if cfg.benchmark in panels.closes and not equity.empty:
        series = panels.closes[cfg.benchmark].reindex(equity.index).dropna()
        if not series.empty:
            bench = cfg.starting_equity * series / series.iloc[0]

    regime_days: dict[str, int] = {}
    for window in windows:
        for verdict, count in (window.test_report.get("regime_days") or {}).items():
            regime_days[verdict] = regime_days.get(verdict, 0) + count

    stub = BacktestResult(trades=trades, equity=equity, benchmark_equity=bench,
                          regime_days=regime_days, config=cfg)
    report = performance(stub)
    report["windows"] = len(windows)
    report["out_of_sample_only"] = True
    return report
