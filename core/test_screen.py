"""Tests for the deterministic layer.

Indicator values are checked against numbers computed by hand in the docstrings,
not against the implementation's own output -- a test that just records what the
code currently does catches nothing.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import indicators as ind                      # noqa: E402
from core import synthetic as syn                       # noqa: E402
from core.ranking import RankConfig, score, top_candidates   # noqa: E402
from core.screen import (CONDITIONS, TEMPLATE_KEYS, ScreenConfig,  # noqa: E402
                         evaluate, run_screen)


def bars_from_closes(closes, **kw):
    return syn.make_bars(closes, **kw)


# --------------------------------------------------------------------------- #
# indicators
# --------------------------------------------------------------------------- #

class TestSMA(unittest.TestCase):
    def test_hand_computed(self):
        s = pd.Series([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], dtype="float64")
        # last three are 8, 9, 10 -> mean 9
        self.assertAlmostEqual(ind.sma(s, 3).iloc[-1], 9.0)
        # first two are NaN with a 3-bar window
        self.assertTrue(np.isnan(ind.sma(s, 3).iloc[0]))
        self.assertTrue(np.isnan(ind.sma(s, 3).iloc[1]))
        self.assertAlmostEqual(ind.sma(s, 3).iloc[2], 2.0)   # (1+2+3)/3

    def test_window_longer_than_series_is_all_nan(self):
        s = pd.Series([1.0, 2.0, 3.0])
        self.assertTrue(ind.sma(s, 10).isna().all())

    def test_rejects_zero_window(self):
        with self.assertRaises(ValueError):
            ind.sma(pd.Series([1.0]), 0)


class TestATR(unittest.TestCase):
    """Wilder ATR, worked by hand.

        bar  H     L     C      TR
        1    10    8     9      10-8 = 2          (no prior close)
        2    11    9     10.5   max(2, 2, 0) = 2
        3    12    10.5  11     max(1.5, 1.5, 0) = 1.5
        4    11.5  9     9.5    max(2.5, 0.5, 2) = 2.5

        window 3 -> seed = (2 + 2 + 1.5)/3 = 1.8333333
        bar 4    -> (1.8333333*2 + 2.5)/3   = 2.0555556
    """
    def setUp(self):
        self.bars = pd.DataFrame(
            {"open":  [9.0, 10.0, 11.0, 10.0],
             "high":  [10.0, 11.0, 12.0, 11.5],
             "low":   [8.0, 9.0, 10.5, 9.0],
             "close": [9.0, 10.5, 11.0, 9.5],
             "volume": [1e6] * 4},
            index=pd.bdate_range("2026-01-05", periods=4),
        )

    def test_true_range_values(self):
        tr = ind.true_range(self.bars)
        np.testing.assert_allclose(tr.to_numpy(), [2.0, 2.0, 1.5, 2.5])

    def test_seed_is_simple_mean(self):
        self.assertAlmostEqual(ind.atr(self.bars, 3).iloc[2], 1.8333333333, places=8)

    def test_wilder_smoothing_step(self):
        self.assertAlmostEqual(ind.atr(self.bars, 3).iloc[3], 2.0555555556, places=8)

    def test_warmup_is_nan(self):
        a = ind.atr(self.bars, 3)
        self.assertTrue(a.iloc[:2].isna().all())

    def test_differs_from_simple_rolling_mean(self):
        # If someone "simplifies" this to a rolling mean, this test fails.
        simple = ind.true_range(self.bars).rolling(3).mean().iloc[3]
        self.assertNotAlmostEqual(ind.atr(self.bars, 3).iloc[3], simple, places=6)


class TestRange52Week(unittest.TestCase):
    def test_pct_below_high_hand_computed(self):
        closes = np.full(300, 50.0)
        closes[100] = 100.0     # the high, inside the trailing year
        closes[-1] = 80.0
        s = pd.Series(closes)
        # high over trailing 252 bars is 100, close is 80 -> 20% below
        self.assertAlmostEqual(ind.pct_below_high(s).iloc[-1], 20.0)

    def test_at_new_high_is_zero(self):
        s = pd.Series(np.arange(1, 301, dtype="float64"))
        self.assertAlmostEqual(ind.pct_below_high(s).iloc[-1], 0.0)

    def test_high_rolls_out_of_window(self):
        closes = np.full(600, 50.0)
        closes[10] = 500.0      # far outside the trailing year at the end
        s = pd.Series(closes)
        self.assertAlmostEqual(ind.pct_below_high(s).iloc[-1], 0.0)


class TestRelativeStrength(unittest.TestCase):
    def test_score_hand_computed(self):
        # close doubles every 63 bars -> every quarterly return is exactly 1.0
        # score = 2*1 + 1 + 1 + 1 = 5
        n = 4 * ind.QUARTER + 1
        s = pd.Series(2.0 ** (np.arange(n) / ind.QUARTER))
        self.assertAlmostEqual(ind.rs_score(s).iloc[-1], 5.0, places=9)

    def test_score_nan_without_full_year(self):
        s = pd.Series(np.linspace(10, 20, 4 * ind.QUARTER - 5))
        self.assertTrue(np.isnan(ind.rs_score(s).iloc[-1]))

    def test_rank_maps_to_1_99(self):
        scores = pd.Series({"A": 1.0, "B": 2.0, "C": 3.0, "D": 4.0})
        ranked = ind.rs_rank(scores)
        # rank(pct) = .25 .5 .75 1.0  ->  *98 + 1
        np.testing.assert_allclose(ranked.to_numpy(), [25.5, 50.0, 74.5, 99.0])

    def test_nan_scores_stay_nan(self):
        scores = pd.Series({"A": 1.0, "B": np.nan, "C": 3.0})
        ranked = ind.rs_rank(scores)
        self.assertTrue(np.isnan(ranked["B"]))
        self.assertFalse(np.isnan(ranked["A"]))

    def test_single_symbol_universe(self):
        self.assertAlmostEqual(ind.rs_rank(pd.Series({"A": 2.0}))["A"], 50.0)


class TestValidation(unittest.TestCase):
    def setUp(self):
        self.good = syn.stage2(n=300)

    def test_accepts_good_bars(self):
        ind.validate_bars(self.good, "OK")

    def test_rejects_missing_column(self):
        with self.assertRaisesRegex(ValueError, "missing column"):
            ind.validate_bars(self.good.drop(columns=["volume"]), "X")

    def test_rejects_unsorted(self):
        with self.assertRaisesRegex(ValueError, "oldest-first"):
            ind.validate_bars(self.good.iloc[::-1], "X")

    def test_rejects_duplicate_dates(self):
        dupe = pd.concat([self.good, self.good.iloc[[-1]]])
        with self.assertRaisesRegex(ValueError, "duplicate"):
            ind.validate_bars(dupe, "X")

    def test_rejects_high_below_low(self):
        bad = self.good.copy()
        bad.iloc[5, bad.columns.get_loc("high")] = 0.01
        with self.assertRaisesRegex(ValueError, "high < low"):
            ind.validate_bars(bad, "X")

    def test_rejects_non_positive_price(self):
        bad = self.good.copy()
        bad.iloc[5, bad.columns.get_loc("low")] = -1.0
        bad.iloc[5, bad.columns.get_loc("high")] = 100.0
        with self.assertRaisesRegex(ValueError, "non-positive"):
            ind.validate_bars(bad, "X")


class TestLookahead(unittest.TestCase):
    def test_future_bars_do_not_change_past_metrics(self):
        ind.assert_no_lookahead(syn.stage2(n=400, noise=0.01, seed=3), "SYNTH")

    def test_as_of_truncates(self):
        bars = syn.stage2(n=400)
        cut = bars.index[-50]
        row = ind.compute_symbol_metrics(bars, "X", as_of=cut)
        self.assertEqual(row["date"], cut)
        self.assertEqual(row["bars_available"], 351)   # index[-50] is the 351st bar

    def test_insufficient_history_returns_none(self):
        self.assertIsNone(ind.compute_symbol_metrics(syn.stage2(n=200), "SHORT"))


# --------------------------------------------------------------------------- #
# screen conditions, one at a time
# --------------------------------------------------------------------------- #

PASSING_ROW = {
    "close": 100.0, "ma50": 95.0, "ma150": 90.0, "ma200": 85.0,
    "ma200_prior": 80.0, "ma200_slope_pct": 6.25,
    "pct_below_52wk_high": 5.0, "pct_above_52wk_low": 120.0,
    "rs_score": 2.0, "rs_rank": 88.0, "atr": 3.0, "atr_pct": 3.0,
    "avg_volume": 1_500_000.0, "avg_dollar_volume": 150_000_000.0,
    "volume_trend": 1.2, "bars_available": 400,
}

# What to change to break exactly one condition, and nothing else.
BREAKERS = {
    "price_floor":      {"close": 2.0, "ma50": 1.5, "ma150": 1.2, "ma200": 1.0,
                         "ma200_prior": 0.9, "pct_below_52wk_high": 5.0},
    "liquidity":        {"avg_volume": 50_000.0},
    "c1_above_ma50":    {"ma50": 105.0},
    "c2_above_ma150":   {"ma150": 105.0, "ma50": 110.0},
    "c3_above_ma200":   {"ma200": 105.0, "ma150": 110.0, "ma50": 115.0},
    "c4_ma50_gt_ma150": {"ma50": 88.0},
    "c5_ma150_gt_ma200": {"ma150": 84.0},
    "c6_ma200_rising":  {"ma200_prior": 90.0},
    "c7_near_52wk_high": {"pct_below_52wk_high": 40.0},
    "c8_rs_rank":       {"rs_rank": 55.0},
}


def frame(**overrides) -> pd.DataFrame:
    row = dict(PASSING_ROW)
    row.update(overrides)
    return pd.DataFrame([row], index=pd.Index(["TEST"], name="symbol"))


class TestConditions(unittest.TestCase):
    def setUp(self):
        self.cfg = ScreenConfig()

    def test_clean_row_passes_everything(self):
        res = evaluate(frame(), self.cfg)
        self.assertTrue(bool(res.evaluated["passes"].iloc[0]))
        self.assertEqual(int(res.evaluated["conditions_met"].iloc[0]), 8)

    def test_each_condition_can_fail_alone(self):
        for key, override in BREAKERS.items():
            with self.subTest(condition=key):
                res = evaluate(frame(**override), self.cfg)
                row = res.evaluated.iloc[0]
                self.assertFalse(bool(row[key]), f"{key} should have failed")
                self.assertFalse(bool(row["passes"]), f"{key} broke but passes stayed True")

    # Conditions 2 and 3 are implied by others and so cannot be broken in
    # isolation -- see test_template_conditions_are_not_independent below.
    INDEPENDENTLY_BREAKABLE = ["c1_above_ma50", "c4_ma50_gt_ma150", "c5_ma150_gt_ma200",
                               "c6_ma200_rising", "c7_near_52wk_high", "c8_rs_rank"]

    def test_breaking_one_template_condition_leaves_seven_met(self):
        for key in self.INDEPENDENTLY_BREAKABLE:
            with self.subTest(condition=key):
                res = evaluate(frame(**BREAKERS[key]), self.cfg)
                self.assertEqual(int(res.evaluated["conditions_met"].iloc[0]), 7,
                                 f"{key} broke more than one condition")

    def test_template_conditions_are_not_independent(self):
        """Two of the eight conditions are logically redundant.

        c1 (close > MA50) and c4 (MA50 > MA150) together imply c2 (close > MA150).
        c2 and c5 (MA150 > MA200) together imply c3 (close > MA200).

        So the "eight conditions" are really six independent constraints plus two
        that can only fail when something else already has. This is worth knowing
        for two reasons: the funnel will never show c2 or c3 eliminating anything
        on their own, and anyone tempted to build a "6 of 8" scored variant would
        be double-counting the moving-average stack. It is not a bug in the
        template -- Minervini states them separately for clarity -- but the
        screen's behaviour only makes sense once you know it.
        """
        # Try to break c2 alone: close below MA150 while still above MA50.
        # Impossible without also violating c1 or c4.
        res = evaluate(frame(**BREAKERS["c2_above_ma150"]), self.cfg)
        row = res.evaluated.iloc[0]
        self.assertFalse(bool(row["c2_above_ma150"]))
        self.assertLess(int(row["conditions_met"]), 7,
                        "c2 should be impossible to break in isolation")

        # And confirm the implication directly.
        implied = frame(close=100.0, ma50=95.0, ma150=90.0)   # c1 and c4 hold
        self.assertTrue(bool(evaluate(implied, self.cfg).evaluated["c2_above_ma150"].iloc[0]))

    def test_nan_never_passes(self):
        for column in ("ma50", "ma200", "rs_rank", "pct_below_52wk_high", "avg_volume"):
            with self.subTest(column=column):
                res = evaluate(frame(**{column: np.nan}), self.cfg)
                self.assertFalse(bool(res.evaluated["passes"].iloc[0]))

    def test_rs_rank_boundary_is_exclusive(self):
        # config says "> 70", so exactly 70 must fail
        self.assertFalse(bool(evaluate(frame(rs_rank=70.0), self.cfg).evaluated["passes"].iloc[0]))
        self.assertTrue(bool(evaluate(frame(rs_rank=70.01), self.cfg).evaluated["passes"].iloc[0]))

    def test_52wk_high_boundary_is_inclusive(self):
        # "within 25%", so exactly 25 must pass
        self.assertTrue(bool(evaluate(frame(pct_below_52wk_high=25.0),
                                      self.cfg).evaluated["passes"].iloc[0]))
        self.assertFalse(bool(evaluate(frame(pct_below_52wk_high=25.01),
                                       self.cfg).evaluated["passes"].iloc[0]))

    def test_all_eight_template_conditions_are_present(self):
        self.assertEqual(len(TEMPLATE_KEYS), 8)
        self.assertEqual(len([c for c in CONDITIONS if c.gate]), 2)

    def test_empty_metrics_frame(self):
        empty = pd.DataFrame(columns=list(PASSING_ROW)).set_index(
            pd.Index([], name="symbol"))
        res = evaluate(empty, self.cfg)
        self.assertTrue(res.passing.empty)
        self.assertEqual(res.summary()["passed_template"], 0)


class TestConfig(unittest.TestCase):
    def test_loads_from_yaml(self):
        cfg = ScreenConfig.from_yaml()
        self.assertEqual(cfg.min_price, 5.00)
        self.assertEqual(cfg.min_avg_volume, 14_000)   # feed units, see screen.yaml
        self.assertEqual(cfg.max_pct_below_52wk_high, 25.0)
        self.assertEqual(cfg.min_rs_rank, 70.0)
        self.assertEqual(cfg.max_candidates, 20)

    def test_rank_weights_sum_to_one(self):
        self.assertAlmostEqual(sum(RankConfig.from_yaml().weights.values()), 1.0)


# --------------------------------------------------------------------------- #
# end to end
# --------------------------------------------------------------------------- #

class TestUniverse(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bars = syn.universe()
        cls.result = run_screen(cls.bars, config=ScreenConfig())

    def test_strongest_leaders_pass(self):
        passing = set(self.result.passing.index)
        for i in (1, 2, 3):
            self.assertIn(f"LEAD{i}", passing)

    def test_no_false_positives(self):
        """Nothing that is not a genuine uptrend gets through."""
        for symbol in self.result.passing.index:
            self.assertTrue(symbol.startswith("LEAD"),
                            f"{symbol} passed the screen but should not have")

    def test_rs_rank_is_relative_to_the_universe(self):
        """The weaker leaders fail c8, not because they are broken but because
        RS rank is a percentile against everything else being screened.

        This is the behaviour to remember when the S&P 500 replaces this
        14-name synthetic universe: `min_rs_rank: 70` always admits roughly the
        top 30% of whatever you feed it, so the size and composition of the
        universe is itself a screen parameter. Change the universe and you have
        changed the screen, even though config/screen.yaml did not move."""
        weak = self.result.evaluated.loc["LEAD6"]
        self.assertFalse(bool(weak["c8_rs_rank"]))
        self.assertTrue(bool(weak["c1_above_ma50"]))     # the trend itself is fine
        self.assertTrue(bool(weak["c6_ma200_rising"]))

    def test_downtrends_and_flats_rejected(self):
        passing = set(self.result.passing.index)
        for sym in ("FALLER1", "FALLER2", "FLAT1", "FLAT2"):
            self.assertNotIn(sym, passing)

    def test_broken_leader_rejected_on_52wk_condition(self):
        # The point of c7: strong MAs are not enough if it is far off its high.
        for sym in ("BROKEN1", "BROKEN2"):
            self.assertNotIn(sym, set(self.result.passing.index))
            self.assertFalse(bool(self.result.evaluated.loc[sym, "c7_near_52wk_high"]))

    def test_spike_high_rejected_on_c7_alone(self):
        """The case condition 7 exists for.

        A one-day squeeze sets a 52-week high the stock never revisits. It
        barely moves a 200-day average, so every other condition still passes --
        this name fails c7 and only c7. Without condition 7 the screen would
        buy it.
        """
        row = self.result.evaluated.loc["SPIKED"]
        failed = [k for k in TEMPLATE_KEYS if not bool(row[k])]
        self.assertEqual(failed, ["c7_near_52wk_high"])
        self.assertGreater(row["pct_below_52wk_high"], 25.0)
        self.assertGreater(row["rs_rank"], 70.0)      # RS is genuinely strong
        self.assertNotIn("SPIKED", set(self.result.passing.index))

    def test_recent_ipo_is_skipped_not_failed(self):
        self.assertIn("NEWIPO", self.result.skipped_symbols)
        self.assertNotIn("NEWIPO", self.result.evaluated.index)

    def test_illiquid_and_penny_fail_gates(self):
        for sym in ("THIN", "PENNY"):
            self.assertFalse(bool(self.result.evaluated.loc[sym, "passes_gates"]))

    def test_funnel_accounts_for_everyone(self):
        funnel = self.result.funnel()
        self.assertEqual(funnel["surviving"].iloc[-1], len(self.result.passing))
        total_eliminated = funnel["eliminated"].sum()
        self.assertEqual(total_eliminated + len(self.result.passing),
                         len(self.result.evaluated))

    def test_implied_conditions_never_eliminate_alone(self):
        """`sole` must be zero for c2 and c3 -- they are implied by others.

        `eliminated` can be non-zero for them because the funnel evaluates in
        Minervini's order and c2 is tested before c4. The first live run showed
        c2 eliminating 46 names, which briefly looked like the redundancy proof
        was wrong. It was not; the documentation of the funnel was.
        """
        funnel = self.result.funnel().set_index("condition")
        self.assertEqual(int(funnel.loc["c2_above_ma150", "sole"]), 0)
        self.assertEqual(int(funnel.loc["c3_above_ma200", "sole"]), 0)

    def test_funnel_reports_both_counts(self):
        self.assertIn("sole", self.result.funnel().columns)
        self.assertIn("eliminated", self.result.funnel().columns)

    def test_summary_as_of_is_a_real_date(self):
        """A live run has no explicit as_of, and used to report the literal
        string "None", which reads like a data bug."""
        as_of = self.result.summary()["as_of"]
        self.assertNotIn("None", as_of)
        self.assertRegex(as_of, r"^\d{4}-\d{2}-\d{2}$")

    def test_funnel_is_monotonic(self):
        surviving = self.result.funnel()["surviving"].tolist()
        self.assertEqual(surviving, sorted(surviving, reverse=True))

    def test_rejection_reasons_cover_all_failures(self):
        reasons = self.result.rejection_reasons()
        failed = set(self.result.evaluated.index) - set(self.result.passing.index)
        self.assertEqual(set(reasons.index), failed)
        self.assertNotIn("unknown", set(reasons.values))

    def test_summary_arithmetic(self):
        s = self.result.summary()
        self.assertEqual(s["universe_size"], len(self.bars))
        self.assertEqual(s["evaluated"] + s["insufficient_history"], s["universe_size"])

    def test_rs_rank_requires_peers(self):
        """A one-symbol universe scores RS 50 and can never pass c8.

        Not a quirk to work around -- it is the correct behaviour of a
        percentile rank, and it means the screen is undefined for a universe of
        one. It also means any test that needs c8 to pass must supply peers.
        """
        solo = run_screen({"X": syn.stage2(annual_growth=2.0)}, config=ScreenConfig())
        self.assertAlmostEqual(solo.evaluated.loc["X", "rs_rank"], 50.0)
        self.assertFalse(bool(solo.evaluated.loc["X", "c8_rs_rank"]))

        with_peers = run_screen({"X": syn.stage2(annual_growth=2.0), **syn.weak_peers()},
                                config=ScreenConfig())
        self.assertTrue(bool(with_peers.evaluated.loc["X", "c8_rs_rank"]))

    def test_deterministic(self):
        again = run_screen(self.bars, config=ScreenConfig())
        pd.testing.assert_frame_equal(
            self.result.evaluated.sort_index(), again.evaluated.sort_index())

    def test_as_of_gives_a_different_but_valid_answer(self):
        earlier = run_screen(self.bars, as_of=pd.Timestamp("2026-03-02"),
                             config=ScreenConfig())
        self.assertLessEqual(earlier.summary()["evaluated"], self.result.summary()["evaluated"])


class TestRanking(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = run_screen(syn.universe(), config=ScreenConfig())
        cls.top = top_candidates(cls.result)

    def test_returns_only_passing_names(self):
        self.assertTrue(set(self.top.index).issubset(set(self.result.passing.index)))

    def test_sorted_descending_by_score(self):
        scores = self.top["rank_score"].tolist()
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_scores_within_unit_interval(self):
        self.assertTrue((self.top["rank_score"] >= 0).all())
        self.assertTrue((self.top["rank_score"] <= 1).all())

    def test_respects_max_candidates(self):
        cfg = RankConfig(weights={"rs_rank": 1.0}, max_candidates=3)
        self.assertEqual(len(top_candidates(self.result, cfg)), 3)

    def test_strongest_leader_ranks_first(self):
        # LEAD1 has the highest growth rate, so the best RS.
        self.assertEqual(self.top.index[0], "LEAD1")

    def test_missing_metric_scores_zero_not_median(self):
        df = self.result.passing.copy()
        df.loc[df.index[0], "volume_trend"] = np.nan
        scored = score(df, RankConfig(weights={"volume_trend": 1.0}))
        self.assertAlmostEqual(scored.loc[df.index[0], "pct_volume_trend"], 0.0)

    def test_all_equal_metric_collapses_to_half(self):
        df = self.result.passing.copy()
        df["volume_trend"] = 1.0
        scored = score(df, RankConfig(weights={"volume_trend": 1.0}))
        self.assertTrue((scored["pct_volume_trend"] == 0.5).all())

    def test_rejects_weights_that_do_not_sum_to_one(self):
        import tempfile, textwrap, os
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
            fh.write(textwrap.dedent("""
                ranking:
                  weights: {rs_rank: 0.5, volume_trend: 0.2}
            """))
            path = fh.name
        try:
            with self.assertRaisesRegex(ValueError, "sum to 1.0"):
                RankConfig.from_yaml(path)
        finally:
            os.unlink(path)

    def test_rejects_unknown_weight_key(self):
        import tempfile, textwrap, os
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
            fh.write(textwrap.dedent("""
                ranking:
                  weights: {moon_phase: 1.0}
            """))
            path = fh.name
        try:
            with self.assertRaisesRegex(ValueError, "unknown ranking weight"):
                RankConfig.from_yaml(path)
        finally:
            os.unlink(path)

    def test_empty_result_ranks_empty(self):
        empty = run_screen({"NEWIPO": syn.recent_ipo()}, config=ScreenConfig())
        self.assertTrue(top_candidates(empty).empty)


if __name__ == "__main__":
    unittest.main(verbosity=1)
