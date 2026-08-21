"""Backtest tests.

The most important test in this file is `test_panels_match_production_metrics`.
Everything else checks the engine's arithmetic; that one checks the engine is
simulating the strategy we actually run, rather than a vectorized lookalike.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtest.engine import (BacktestConfig, Trade, _check_exit,  # noqa: E402
                             run_backtest)
from backtest.metrics import (cagr, drawdown_duration_days,  # noqa: E402
                              max_drawdown, performance, sharpe, trade_stats)
from backtest.panels import build_panels                        # noqa: E402
from backtest.walkforward import _apply, _grid, _stitch, make_windows, walk_forward  # noqa: E402
from core import indicators as ind                              # noqa: E402
from core import synthetic as syn                               # noqa: E402


class TestPanels(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bars, cls.sectors = syn.market(n_symbols=12, n_days=700, seed=3)
        cls.panels = build_panels(cls.bars, sectors=cls.sectors)

    def test_shape(self):
        self.assertEqual(len(self.panels.symbols), 13)      # 12 + SPY
        self.assertGreater(len(self.panels.dates), 600)

    def test_panels_match_production_metrics(self):
        """A panel row must equal `compute_symbol_metrics` on the same date.

        This is the test that stops the backtest from measuring a different
        strategy than the one that runs at 05:00. If anyone vectorizes an
        indicator for speed and gets it subtly wrong, this fails.
        """
        rng = np.random.default_rng(0)
        dates = self.panels.dates[-120:]
        checked = 0
        for symbol in list(self.bars)[:6]:
            for when in rng.choice(dates, size=4, replace=False):
                when = pd.Timestamp(when)
                expected = ind.compute_symbol_metrics(self.bars[symbol], symbol, as_of=when)
                if expected is None:
                    continue
                row = self.panels.metrics_on(when).loc[symbol]
                for field in ("close", "ma50", "ma150", "ma200", "ma200_prior",
                              "ma200_slope_pct", "pct_below_52wk_high", "rs_score",
                              "atr", "atr_pct", "avg_volume", "volume_trend"):
                    a, b = expected[field], float(row[field])
                    with self.subTest(symbol=symbol, date=str(when.date()), field=field):
                        if np.isnan(a):
                            self.assertTrue(np.isnan(b))
                        else:
                            self.assertAlmostEqual(a, b, places=9)
                checked += 1
        self.assertGreater(checked, 10, "the identity check did not actually run")

    def test_rs_rank_is_cross_sectional_per_date(self):
        when = self.panels.dates[-1]
        metrics = self.panels.metrics_on(when)
        ranks = metrics["rs_rank"].dropna()
        self.assertGreaterEqual(ranks.min(), 1.0)
        self.assertLessEqual(ranks.max(), 99.0)

    def test_metrics_frame_feeds_the_real_screen_unchanged(self):
        from core.screen import ScreenConfig, evaluate
        result = evaluate(self.panels.metrics_on(self.panels.dates[-1]), ScreenConfig())
        self.assertIn("passes", result.evaluated.columns)

    def test_rejects_universe_with_no_history(self):
        with self.assertRaisesRegex(ValueError, "enough history"):
            build_panels({"X": syn.stage2(n=50)})


class TestExitLogic(unittest.TestCase):
    def setUp(self):
        idx = pd.bdate_range("2026-01-05", periods=5)
        self.panels = type("P", (), {})()
        self.panels.opens = pd.DataFrame({"X": [100, 100, 100, 100, 100.0]}, index=idx)
        self.panels.highs = pd.DataFrame({"X": [101, 101, 101, 101, 101.0]}, index=idx)
        self.panels.lows = pd.DataFrame({"X": [99, 99, 99, 99, 99.0]}, index=idx)
        self.panels.closes = pd.DataFrame({"X": [100, 100, 100, 100, 100.0]}, index=idx)
        self.idx = idx
        self.cfg = BacktestConfig(max_hold_days=3)
        self.trade = Trade(symbol="X", entry_date=idx[0], entry_price=100.0,
                           shares=10, stop=96.0, target=110.0)

    def test_no_exit_on_the_entry_bar(self):
        self.assertEqual(_check_exit(self.panels, self.trade, self.idx[0], self.cfg),
                         (None, ""))

    def test_stop_hit(self):
        self.panels.lows.loc[self.idx[2], "X"] = 95.0
        price, reason = _check_exit(self.panels, self.trade, self.idx[2], self.cfg)
        self.assertEqual((price, reason), (96.0, "stop"))

    def test_gap_through_the_stop_fills_at_the_open(self):
        """Modelling every stop as filling exactly at the stop price is the
        commonest way a backtest understates drawdown."""
        self.panels.opens.loc[self.idx[2], "X"] = 90.0
        self.panels.lows.loc[self.idx[2], "X"] = 89.0
        price, reason = _check_exit(self.panels, self.trade, self.idx[2], self.cfg)
        self.assertEqual((price, reason), (90.0, "gap_stop"))

    def test_target_hit(self):
        self.panels.highs.loc[self.idx[2], "X"] = 112.0
        price, reason = _check_exit(self.panels, self.trade, self.idx[2], self.cfg)
        self.assertEqual((price, reason), (110.0, "target"))

    def test_stop_wins_when_the_bar_spans_both(self):
        """Intrabar order is unknowable, so we assume the worst. Being
        optimistic here inflates the win rate on exactly the volatile bars
        where it matters most."""
        self.panels.lows.loc[self.idx[2], "X"] = 95.0
        self.panels.highs.loc[self.idx[2], "X"] = 115.0
        _, reason = _check_exit(self.panels, self.trade, self.idx[2], self.cfg)
        self.assertIn("stop", reason)

    def test_time_exit(self):
        price, reason = _check_exit(self.panels, self.trade, self.idx[4], self.cfg)
        self.assertEqual(reason, "time")
        self.assertEqual(price, 100.0)


class TestTradeArithmetic(unittest.TestCase):
    def test_pnl_and_r_multiple(self):
        t = Trade(symbol="X", entry_date=pd.Timestamp("2026-01-05"), entry_price=100.0,
                  shares=10, stop=96.0, target=110.0, costs=2.0)
        t.exit_price, t.exit_date = 110.0, pd.Timestamp("2026-01-20")
        # 10 * (110 - 100) - 2 = 98; risk = 10 * 4 = 40 -> 2.45R
        self.assertAlmostEqual(t.pnl, 98.0)
        self.assertAlmostEqual(t.r_multiple, 2.45)

    def test_loss_is_about_minus_one_r(self):
        t = Trade(symbol="X", entry_date=pd.Timestamp("2026-01-05"), entry_price=100.0,
                  shares=10, stop=96.0, target=110.0, costs=0.0)
        t.exit_price, t.exit_date = 96.0, pd.Timestamp("2026-01-08")
        self.assertAlmostEqual(t.r_multiple, -1.0)

    def test_open_trade_has_no_pnl(self):
        t = Trade(symbol="X", entry_date=pd.Timestamp("2026-01-05"), entry_price=100.0,
                  shares=10, stop=96.0, target=110.0)
        self.assertTrue(t.open)
        self.assertEqual(t.pnl, 0.0)


class TestMetrics(unittest.TestCase):
    def test_cagr_hand_computed(self):
        # doubling over exactly one year of trading days
        equity = pd.Series(np.linspace(100, 200, 252),
                           index=pd.bdate_range("2025-01-01", periods=252))
        self.assertAlmostEqual(cagr(equity), 1.0, places=2)

    def test_max_drawdown_hand_computed(self):
        equity = pd.Series([100, 120, 90, 130], index=pd.bdate_range("2026-01-05", periods=4))
        self.assertAlmostEqual(max_drawdown(equity), -0.25)      # 120 -> 90

    def test_drawdown_duration(self):
        equity = pd.Series([100, 90, 95, 99, 101],
                           index=pd.bdate_range("2026-01-05", periods=5))
        self.assertEqual(drawdown_duration_days(equity), 3)

    def test_flat_equity_has_zero_drawdown_and_nan_sharpe(self):
        equity = pd.Series([100.0] * 50, index=pd.bdate_range("2026-01-05", periods=50))
        self.assertAlmostEqual(max_drawdown(equity), 0.0)
        self.assertTrue(np.isnan(sharpe(equity)))

    def test_no_trades_returns_a_complete_dict(self):
        """A strategy that takes no trades is a result, not an error -- the
        report must render rather than raise."""
        stats = trade_stats([])
        for key in ("trades", "win_rate", "expectancy_r", "total_costs",
                    "exit_reasons", "avg_hold_days", "profit_factor"):
            self.assertIn(key, stats)


class TestWalkForward(unittest.TestCase):
    def test_windows_do_not_overlap_in_test(self):
        dates = pd.bdate_range("2020-01-01", periods=1500)
        windows = make_windows(dates, train_days=504, test_days=126)
        self.assertGreater(len(windows), 3)
        for a, b in zip(windows, windows[1:]):
            self.assertLessEqual(a.test_end, b.test_start)

    def test_train_always_precedes_test(self):
        dates = pd.bdate_range("2020-01-01", periods=1500)
        for w in make_windows(dates, train_days=504, test_days=126):
            self.assertLess(w.train_end, w.test_start)

    def test_grid_expansion(self):
        self.assertEqual(len(_grid({"a": [1, 2], "b": [3, 4, 5]})), 6)
        self.assertEqual(_grid({}), [{}])

    def test_apply_dotted_params(self):
        cfg = BacktestConfig()
        out = _apply(cfg, {"screen.min_rs_rank": 85.0, "sizing.atr_stop_multiple": 3.0})
        self.assertEqual(out.screen.min_rs_rank, 85.0)
        self.assertEqual(out.sizing.atr_stop_multiple, 3.0)
        self.assertEqual(cfg.screen.min_rs_rank, 70.0)      # original untouched

    def test_stitch_chains_by_return_not_level(self):
        """Segments each start at the same equity inside their own window. Naive
        concatenation would reset the curve every window and make CAGR
        meaningless."""
        idx1 = pd.bdate_range("2026-01-05", periods=3)
        idx2 = pd.bdate_range("2026-02-05", periods=3)
        a = pd.Series([100.0, 110.0, 120.0], index=idx1)
        b = pd.Series([100.0, 90.0, 95.0], index=idx2)
        out = _stitch([a, b], 100.0)
        self.assertAlmostEqual(out.iloc[2], 120.0)
        self.assertAlmostEqual(out.iloc[-1], 120.0 * 0.95)

    def test_stitch_handles_empty(self):
        self.assertTrue(_stitch([], 100.0).empty)


class TestEndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        bars, sectors = syn.market(n_symbols=25, n_days=900, seed=11)
        cls.panels = build_panels(bars, sectors=sectors)
        cls.result = run_backtest(cls.panels, BacktestConfig(starting_equity=25_000))
        cls.report = performance(cls.result)

    def test_it_traded(self):
        self.assertGreater(len(self.result.trades), 5)

    def test_all_three_regimes_are_reachable(self):
        """An engine that can never reach RISK_ON silently halves risk on every
        run -- which is exactly what an earlier version did."""
        self.assertGreaterEqual(len(self.result.regime_days), 2)

    def test_no_entry_before_signal_date(self):
        for trade in self.result.trades:
            self.assertIn(trade.entry_date, self.panels.dates)

    def test_exit_never_precedes_entry(self):
        for trade in self.result.closed:
            self.assertGreater(trade.exit_date, trade.entry_date)

    def test_costs_are_charged(self):
        self.assertGreater(self.report["total_costs"], 0)

    def test_zero_slippage_beats_realistic_slippage(self):
        """If costs make no difference, they are not being applied."""
        cheap = performance(run_backtest(
            self.panels, BacktestConfig(starting_equity=25_000, slippage_pct=0.0)))
        dear = performance(run_backtest(
            self.panels, BacktestConfig(starting_equity=25_000, slippage_pct=0.5)))
        self.assertGreater(cheap["ending_equity"], dear["ending_equity"])

    def test_benchmark_null_is_always_reported(self):
        self.assertIsNotNone(self.report["benchmark"])
        self.assertIn("excess_return_pct", self.report)

    def test_equity_curve_is_continuous(self):
        self.assertEqual(len(self.result.equity), len(self.result.equity.dropna()))
        self.assertTrue((self.result.equity > 0).all())

    def test_regime_filter_changes_the_outcome(self):
        off = performance(run_backtest(self.panels, BacktestConfig(
            starting_equity=25_000, use_regime_filter=False)))
        self.assertNotEqual(off["trades"], self.report["trades"])

    def test_deterministic(self):
        again = performance(run_backtest(self.panels, BacktestConfig(starting_equity=25_000)))
        self.assertEqual(again["ending_equity"], self.report["ending_equity"])

    def test_walk_forward_reports_out_of_sample_only(self):
        wf = walk_forward(self.panels, BacktestConfig(starting_equity=25_000),
                          {"screen.min_rs_rank": [65.0, 75.0]},
                          train_days=400, test_days=120)
        self.assertTrue(wf.report["out_of_sample_only"])
        self.assertGreaterEqual(wf.report["windows"], 1)
        for w in wf.windows:
            self.assertLess(w.train_end, w.test_start)

    def test_walk_forward_needs_enough_history(self):
        with self.assertRaisesRegex(ValueError, "not enough history"):
            walk_forward(self.panels, BacktestConfig(), {}, train_days=5000, test_days=500)


if __name__ == "__main__":
    unittest.main(verbosity=1)
