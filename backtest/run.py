#!/usr/bin/env python3
"""Backtest CLI.

    python3 -m backtest.run --offline                    # synthetic universe
    python3 -m backtest.run                              # cached real bars
    python3 -m backtest.run --walk-forward               # the honest version
    python3 -m backtest.run --no-regime                  # what the filter costs

Read docs/backtest.md before believing a number this prints.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd  # noqa: E402

from backtest.engine import BacktestConfig, run_backtest        # noqa: E402
from backtest.metrics import format_report, performance         # noqa: E402
from backtest.panels import build_panels                        # noqa: E402
from backtest.walkforward import walk_forward                   # noqa: E402
from core.ranking import RankConfig                             # noqa: E402
from core.regime import RegimeConfig                            # noqa: E402
from core.screen import ScreenConfig                            # noqa: E402
from core.sizing import SizingConfig                            # noqa: E402

log = logging.getLogger("backtest")

# Kept small on purpose: a wide grid searched on a short history is a machine
# for manufacturing overfitting. Two axes, three values each.
DEFAULT_GRID = {
    "screen.min_rs_rank": [60.0, 70.0, 80.0],
    "sizing.atr_stop_multiple": [1.5, 2.0, 2.5],
}


def load_bars(offline: bool, symbols: int, years: int) -> tuple[dict, dict]:
    if offline:
        from core import synthetic as syn
        # A correlated market with bull/bear phases, not independent stocks --
        # see synthetic.market() for why that distinction decides whether the
        # regime filter is exercised at all.
        return syn.market(n_symbols=symbols, n_days=years * 252)

    from mcp.market_data import BarCache
    from mcp.market_data.universe import load_sectors
    from pipeline import paths

    cache = BarCache(paths.BARS_DB)
    import sqlite3
    from contextlib import closing
    with closing(sqlite3.connect(cache.path)) as conn:
        names = [r[0] for r in conn.execute("SELECT DISTINCT symbol FROM bars")]
    if not names:
        raise SystemExit(
            "the bar cache is empty. Run `python3 -m pipeline.stage_a_nightly` first, "
            "or pass --offline.")
    bars = {s: b for s, b in cache.read_many(names).items() if not b.empty}
    return bars, load_sectors()


def build_config(args) -> BacktestConfig:
    return BacktestConfig(
        starting_equity=args.equity,
        max_hold_days=args.max_hold,
        slippage_pct=args.slippage,
        use_regime_filter=not args.no_regime,
        rebalance_every=args.rebalance,
        screen=ScreenConfig.from_yaml(),
        rank=RankConfig.from_yaml(),
        sizing=SizingConfig.from_yaml(),
        regime=RegimeConfig.from_yaml(),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Backtest the swing screen")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--symbols", type=int, default=80, help="offline universe size")
    parser.add_argument("--years", type=int, default=6, help="offline history length")
    parser.add_argument("--equity", type=float, default=25_000)
    parser.add_argument("--slippage", type=float, default=0.05,
                        help="percent per side")
    parser.add_argument("--max-hold", type=int, default=40)
    parser.add_argument("--rebalance", type=int, default=1,
                        help="trading days between screens")
    parser.add_argument("--no-regime", action="store_true",
                        help="disable the regime filter to measure what it costs")
    parser.add_argument("--walk-forward", action="store_true")
    parser.add_argument("--train-days", type=int, default=504)
    parser.add_argument("--test-days", type=int, default=126)
    parser.add_argument("--no-grid", action="store_true",
                        help="walk forward without parameter selection")
    parser.add_argument("--start", type=date.fromisoformat, default=None)
    parser.add_argument("--end", type=date.fromisoformat, default=None)
    parser.add_argument("--json", type=Path, default=None, help="write the report here")
    parser.add_argument("--trades-csv", type=Path, default=None)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S")

    bars, sectors = load_bars(args.offline, args.symbols, args.years)
    log.info("loaded %d symbols", len(bars))

    panels = build_panels(bars, sectors=sectors,
                          on_progress=lambda i, n: log.info("  panels %d/%d", i, n))
    log.info("panels: %d dates x %d symbols", len(panels.dates), len(panels.symbols))

    cfg = build_config(args)
    if args.start:
        cfg.start = pd.Timestamp(args.start)
    if args.end:
        cfg.end = pd.Timestamp(args.end)

    if args.walk_forward:
        grid = {} if args.no_grid else DEFAULT_GRID
        wf = walk_forward(panels, cfg, grid,
                          train_days=args.train_days, test_days=args.test_days,
                          on_progress=lambda i, n: log.info("  window %d/%d", i, n))
        print(format_report(wf.report, "WALK-FORWARD (out-of-sample only)"))
        print(f"\n  windows           {len(wf.windows)}")
        if wf.overfitting_gap is not None:
            print(f"  overfitting gap   {wf.overfitting_gap:+.3f}R "
                  f"(in-sample minus out-of-sample expectancy)")
            if wf.overfitting_gap > 0.3:
                print("  ^^ large gap: the parameter search is fitting noise")
        print("\n  per-window:")
        for w in wf.windows:
            d = w.to_dict()
            print(f"    {d['test'][0]} -> {d['test'][1]}  "
                  f"{str(d['chosen']):46} "
                  f"test {d['test_expectancy_r']}R over {d['test_trades']} trades")
        report, trades = wf.report, wf.oos_trades
    else:
        result = run_backtest(panels, cfg,
                              on_progress=lambda i, n: log.info("  day %d/%d", i, n))
        report = performance(result)
        trades = result.trades
        print(format_report(report, "IN-SAMPLE BACKTEST — not evidence, see docs"))
        print("\n  This is a single-split run over the whole history. It is a "
              "\n  smoke test, not a result. Use --walk-forward for anything you "
              "\n  intend to act on.")

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(report, indent=2, default=str))
        log.info("wrote %s", args.json)

    if args.trades_csv and trades:
        frame = pd.DataFrame([t.to_dict() for t in trades])
        args.trades_csv.parent.mkdir(parents=True, exist_ok=True)
        frame.to_csv(args.trades_csv, index=False)
        log.info("wrote %s (%d trades)", args.trades_csv, len(frame))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
