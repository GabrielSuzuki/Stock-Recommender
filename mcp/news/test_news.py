"""Disqualify-rule and news-provider tests."""
from __future__ import annotations

import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from mcp.news import NewsContext, OfflineNewsProvider, check_disqualifiers  # noqa: E402
from mcp.news.disqualify import upcoming_earnings_note                       # noqa: E402

TODAY = date(2026, 8, 19)


def ctx(headlines=None, earnings=None, filings=None) -> NewsContext:
    return NewsContext(
        symbol="TEST",
        headlines=[{"headline": h, "summary": ""} for h in (headlines or [])],
        earnings_date=earnings, filings=filings or [])


class TestEarningsRule(unittest.TestCase):
    def test_earnings_inside_window_disqualifies(self):
        r = check_disqualifiers(ctx(earnings=TODAY + timedelta(days=7)), as_of=TODAY)
        self.assertTrue(r.disqualified)
        self.assertIn("earnings_in_window", r.rules_hit)

    def test_earnings_on_the_boundary_disqualifies(self):
        r = check_disqualifiers(ctx(earnings=TODAY + timedelta(days=21)),
                                as_of=TODAY, hold_days=21)
        self.assertTrue(r.disqualified)

    def test_earnings_one_day_past_the_window_is_fine(self):
        r = check_disqualifiers(ctx(earnings=TODAY + timedelta(days=22)),
                                as_of=TODAY, hold_days=21)
        self.assertFalse(r.disqualified)

    def test_earnings_today_disqualifies(self):
        self.assertTrue(check_disqualifiers(ctx(earnings=TODAY), as_of=TODAY).disqualified)

    def test_past_earnings_does_not_disqualify(self):
        r = check_disqualifiers(ctx(earnings=TODAY - timedelta(days=3)), as_of=TODAY)
        self.assertFalse(r.disqualified)

    def test_unknown_earnings_does_not_disqualify(self):
        self.assertFalse(check_disqualifiers(ctx(), as_of=TODAY).disqualified)

    def test_note_for_earnings_beyond_the_window(self):
        note = upcoming_earnings_note(ctx(earnings=TODAY + timedelta(days=45)), TODAY)
        self.assertIn("45d out", note)

    def test_no_note_when_inside_the_window(self):
        self.assertIsNone(
            upcoming_earnings_note(ctx(earnings=TODAY + timedelta(days=5)), TODAY))


class TestFilingRules(unittest.TestCase):
    def test_424b_takedown_disqualifies(self):
        r = check_disqualifiers(
            ctx(filings=[{"form": "424B5", "filed": "2026-08-18"}]), as_of=TODAY)
        self.assertTrue(r.disqualified)
        self.assertIn("dilution_filing", r.rules_hit)

    def test_s3_shelf_disqualifies(self):
        r = check_disqualifiers(ctx(filings=[{"form": "S-3", "filed": "2026-08-10"}]),
                                as_of=TODAY)
        self.assertIn("dilution_filing", r.rules_hit)

    def test_going_private_disqualifies(self):
        r = check_disqualifiers(ctx(filings=[{"form": "SC 13E3", "filed": "2026-08-10"}]),
                                as_of=TODAY)
        self.assertIn("going_private", r.rules_hit)

    def test_ordinary_8k_does_not_disqualify(self):
        r = check_disqualifiers(ctx(filings=[{"form": "8-K", "filed": "2026-08-18"}]),
                                as_of=TODAY)
        self.assertFalse(r.disqualified)


class TestHeadlineRules(unittest.TestCase):
    CASES = {
        "reverse_split": ["Acme announces 1-for-10 reverse stock split",
                          "Board approves reverse split"],
        "going_concern": ["Auditor raises going-concern doubt",
                          "Filing cites substantial doubt about continuing"],
        "acquisition_fixed_price": ["Acme to be acquired for $45 a share",
                                    "Signs definitive merger agreement"],
        "offering": ["Announces public offering of 10m shares",
                     "Launches at-the-market program"],
        "bankruptcy": ["Files for Chapter 11 protection"],
        "delisting": ["Receives delisting notice from the exchange"],
    }

    def test_each_pattern_fires(self):
        for rule, headlines in self.CASES.items():
            for headline in headlines:
                with self.subTest(rule=rule, headline=headline):
                    r = check_disqualifiers(ctx([headline]), as_of=TODAY)
                    self.assertTrue(r.disqualified, headline)
                    self.assertIn(rule, r.rules_hit)

    def test_benign_headlines_do_not_fire(self):
        benign = ["Acme raises full-year guidance",
                  "Analyst initiates coverage at Buy",
                  "Acme opens a new distribution centre",
                  "Acme announces $500m buyback",
                  "Acme names new chief financial officer",
                  "Quarterly revenue up 22% year over year"]
        for headline in benign:
            with self.subTest(headline=headline):
                self.assertFalse(check_disqualifiers(ctx([headline]), as_of=TODAY).disqualified,
                                 headline)

    def test_forward_split_is_not_a_reverse_split(self):
        """A 4-for-1 forward split is bullish housekeeping; only the reverse
        kind is disqualifying. Getting this backwards would drop good names."""
        r = check_disqualifiers(ctx(["Acme announces 4-for-1 stock split"]), as_of=TODAY)
        self.assertNotIn("reverse_split", r.rules_hit)

    def test_multiple_rules_all_reported(self):
        r = check_disqualifiers(
            ctx(["Announces reverse stock split", "Files for Chapter 11"],
                earnings=TODAY + timedelta(days=3)), as_of=TODAY)
        self.assertGreaterEqual(len(r.rules_hit), 3)
        self.assertIn(";", r.summary)

    def test_summary_is_empty_when_clean(self):
        self.assertEqual(check_disqualifiers(ctx(["All is well"]), as_of=TODAY).summary, "")


class TestOfflineProvider(unittest.TestCase):
    def test_deterministic(self):
        a = OfflineNewsProvider().context("AAA", as_of=TODAY)
        b = OfflineNewsProvider().context("AAA", as_of=TODAY)
        self.assertEqual([h["headline"] for h in a.headlines],
                         [h["headline"] for h in b.headlines])

    def test_produces_some_disqualifications_across_a_universe(self):
        """The offline path must exercise the disqualify branch, or that code
        is only ever tested when live news happens to cooperate."""
        provider = OfflineNewsProvider()
        hits = sum(
            check_disqualifiers(provider.context(f"SYN{i:03d}", as_of=TODAY),
                                as_of=TODAY).disqualified
            for i in range(40))
        self.assertGreater(hits, 0)
        self.assertLess(hits, 40)

    def test_context_serialises(self):
        d = OfflineNewsProvider().context("AAA", as_of=TODAY).to_dict()
        for key in ("symbol", "headlines", "earnings_date", "filings", "errors"):
            self.assertIn(key, d)

    def test_degraded_flag(self):
        self.assertFalse(OfflineNewsProvider().context("AAA", as_of=TODAY).degraded)
        self.assertTrue(NewsContext(symbol="X", errors=["boom"]).degraded)


if __name__ == "__main__":
    unittest.main(verbosity=1)
