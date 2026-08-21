"""Weekly review tests — the arithmetic, not the prose."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.weekly_review import (_match_round_trips, analyse,  # noqa: E402
                                    condition_correlation, render)


def brief(as_of: str, regime: str = "RISK_ON", picks=()) -> dict:
    return {"as_of": as_of, "regime": regime,
            "picks": [{"symbol": s, "conviction": c,
                       "plan": {"entry": e}, "thesis": "t"}
                      for s, c, e in picks]}


def fill(symbol: str, side: str, qty: int, price: float, at: str = "2026-08-19T10:00") -> dict:
    return {"symbol": symbol, "side": side, "quantity": qty, "price": price,
            "logged_at": at}


class TestRoundTrips(unittest.TestCase):
    def test_simple_round_trip(self):
        trips = _match_round_trips([fill("AAPL", "buy", 100, 180.0)],
                                   [fill("AAPL", "sell", 100, 190.0)])
        self.assertEqual(len(trips), 1)
        self.assertAlmostEqual(trips[0]["pnl"], 1000.0)
        self.assertAlmostEqual(trips[0]["return_pct"], 5.56, places=2)

    def test_fifo_across_two_lots(self):
        trips = _match_round_trips(
            [fill("A", "buy", 50, 100.0, "2026-08-01T10:00"),
             fill("A", "buy", 50, 110.0, "2026-08-05T10:00")],
            [fill("A", "sell", 75, 120.0, "2026-08-10T10:00")])
        self.assertEqual(len(trips), 2)
        self.assertEqual(trips[0]["quantity"], 50)      # first lot fully
        self.assertEqual(trips[1]["quantity"], 25)      # part of the second
        self.assertAlmostEqual(trips[0]["entry"], 100.0)
        self.assertAlmostEqual(trips[1]["entry"], 110.0)

    def test_unmatched_sell_is_skipped_not_guessed(self):
        trips = _match_round_trips([], [fill("A", "sell", 10, 100.0)])
        self.assertEqual(trips, [])

    def test_open_position_produces_no_round_trip(self):
        self.assertEqual(_match_round_trips([fill("A", "buy", 10, 100.0)], []), [])

    def test_partial_sell_leaves_the_rest_open(self):
        trips = _match_round_trips([fill("A", "buy", 100, 100.0)],
                                   [fill("A", "sell", 40, 110.0)])
        self.assertEqual(len(trips), 1)
        self.assertEqual(trips[0]["quantity"], 40)


class TestAnalyse(unittest.TestCase):
    def test_counts_regime_days_including_risk_off(self):
        """RISK_OFF days are the whole reason the journal records empty briefs
        -- without them the filter's contribution is unmeasurable."""
        out = analyse([brief("2026-08-17", "RISK_OFF"),
                       brief("2026-08-18", "RISK_ON", [("A", 4, 100.0)]),
                       brief("2026-08-19", "NEUTRAL", [("B", 3, 50.0)])], [])
        self.assertEqual(out["risk_off_days"], 1)
        self.assertEqual(out["regime_days"],
                         {"RISK_OFF": 1, "RISK_ON": 1, "NEUTRAL": 1})

    def test_action_rate(self):
        out = analyse([brief("2026-08-19", picks=[("A", 4, 100.0), ("B", 3, 50.0)])],
                      [fill("A", "buy", 10, 101.0)])
        self.assertEqual((out["names_acted_on"], out["names_ignored"]), (1, 1))
        self.assertEqual(out["action_rate_pct"], 50.0)
        self.assertEqual(out["ignored_symbols"], ["B"])

    def test_off_plan_buys_flagged(self):
        out = analyse([brief("2026-08-19", picks=[("A", 4, 100.0)])],
                      [fill("A", "buy", 10, 100.0), fill("ZZZ", "buy", 5, 20.0)])
        self.assertEqual(out["off_plan_buys"], ["ZZZ"])

    def test_entry_slippage_measured_against_the_plan(self):
        out = analyse([brief("2026-08-19", picks=[("A", 4, 100.0)])],
                      [fill("A", "buy", 10, 102.0)])
        self.assertAlmostEqual(out["avg_entry_slippage_pct"], 2.0)

    def test_no_briefs_is_handled(self):
        out = analyse([], [])
        self.assertEqual(out["briefs"], 0)
        self.assertIsNone(out["action_rate_pct"])

    def test_realised_pnl_and_win_rate(self):
        out = analyse(
            [brief("2026-08-19", picks=[("A", 4, 100.0), ("B", 4, 50.0)])],
            [fill("A", "buy", 10, 100.0, "2026-08-01T10:00"),
             fill("A", "sell", 10, 110.0, "2026-08-05T10:00"),
             fill("B", "buy", 10, 50.0, "2026-08-01T10:00"),
             fill("B", "sell", 10, 45.0, "2026-08-05T10:00")])
        self.assertAlmostEqual(out["realised_pnl"], 50.0)      # +100 and -50
        self.assertEqual(out["win_rate_pct"], 50.0)


class TestConditionCorrelation(unittest.TestCase):
    def test_splits_winners_from_losers(self):
        briefs = [brief("2026-08-19", picks=[("A", 5, 100.0), ("B", 2, 50.0)])]
        fills = [fill("A", "buy", 10, 100.0, "2026-08-01T10:00"),
                 fill("A", "sell", 10, 120.0, "2026-08-05T10:00"),
                 fill("B", "buy", 10, 50.0, "2026-08-01T10:00"),
                 fill("B", "sell", 10, 40.0, "2026-08-05T10:00")]
        stats = condition_correlation(briefs, analyse(briefs, fills))
        self.assertEqual(stats["conviction"]["winners_mean"], 5)
        self.assertEqual(stats["conviction"]["losers_mean"], 2)

    def test_empty_when_nothing_closed(self):
        briefs = [brief("2026-08-19", picks=[("A", 5, 100.0)])]
        self.assertEqual(condition_correlation(briefs, analyse(briefs, [])), {})


class TestRender(unittest.TestCase):
    def test_renders_without_optional_fields(self):
        out = analyse([brief("2026-08-19", "RISK_OFF")], [])
        text = render(out, {}, {"summary": "Quiet week.", "observations": []}, 4)
        self.assertIn("WEEKLY REVIEW", text)
        self.assertIn("Quiet week", text)

    def test_flags_off_plan_and_skipped(self):
        briefs = [brief("2026-08-19", picks=[("A", 4, 100.0), ("B", 3, 50.0)])]
        fills = [fill("ZZZ", "buy", 5, 20.0)]
        text = render(analyse(briefs, fills), {},
                      {"summary": "", "observations": [], "suggested_change": "x"}, 1)
        self.assertIn("OFF-PLAN", text)
        self.assertIn("SUGGESTED", text)


if __name__ == "__main__":
    unittest.main(verbosity=1)
