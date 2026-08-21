"""Exit-rule tests. Every expected value is worked by hand."""
from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.exits import (HOLD, SELL, TRIM, WATCH, ExitConfig,  # noqa: E402
                        Position, evaluate_book, evaluate_position, update_stop)

CFG = ExitConfig(max_hold_days=40, breakeven_after_r=1.0, trail_after_r=2.0,
                 trail_atr_multiple=2.5, warn_below_ma=50, warn_rs_below=50.0,
                 scale_out_at_target=True, scale_out_fraction=0.5)

TODAY = date(2026, 8, 20)


def pos(entry=100.0, stop=96.0, target=110.0, shares=100.0,
        entry_date=date(2026, 8, 10), **kw) -> Position:
    return Position(symbol="TEST", entry_date=entry_date, entry_price=entry,
                    shares=shares, stop=stop, target=target, **kw)


def quote(close, **kw) -> dict:
    q = {"close": close, "open": close, "high": close, "low": close}
    q.update(kw)
    return q


class TestPositionMath(unittest.TestCase):
    def test_r_multiple(self):
        # entry 100, stop 96 -> risk 4/share. At 108 that is +2R.
        self.assertAlmostEqual(pos().r_multiple(108.0), 2.0)

    def test_r_is_anchored_to_the_initial_stop(self):
        """R must not be recomputed against a trailing stop, or every ratchet
        inflates the reported R and a mediocre trade looks good."""
        p = pos()
        p.stop = 104.0                      # trailed up
        self.assertAlmostEqual(p.risk_per_share, 4.0)
        self.assertAlmostEqual(p.r_multiple(108.0), 2.0)

    def test_unrealised(self):
        self.assertAlmostEqual(pos().unrealised(105.0), 500.0)

    def test_roundtrip_serialisation(self):
        p = pos(thesis="t", invalidation="i", sector="Tech")
        self.assertEqual(Position.from_dict(p.to_dict()).to_dict(), p.to_dict())


class TestUpdateStop(unittest.TestCase):
    def test_no_move_below_breakeven_threshold(self):
        # +0.5R: below breakeven_after_r, stop unchanged
        self.assertAlmostEqual(update_stop(pos(), 102.0, 2.0, CFG), 96.0)

    def test_moves_to_breakeven_at_1r(self):
        self.assertAlmostEqual(update_stop(pos(), 104.0, 2.0, CFG), 100.0)

    def test_trails_at_2r(self):
        # +3R at 112. Floor = 100 + 2*4 = 108. ATR trail = 112 - 2.5*2 = 107.
        # min(107, 108) = 107, and max against current stop.
        self.assertAlmostEqual(update_stop(pos(), 112.0, 2.0, CFG), 107.0)

    def test_trail_uses_the_floor_when_atr_is_missing(self):
        self.assertAlmostEqual(update_stop(pos(), 112.0, None, CFG), 108.0)

    def test_never_lowers_the_stop(self):
        """A rule that could lower a stop widens risk on exactly the positions
        that had started working."""
        p = pos()
        p.stop = 106.0                      # already trailed high
        self.assertAlmostEqual(update_stop(p, 108.0, 5.0, CFG), 106.0)

    def test_ratchet_is_monotonic_over_a_price_path(self):
        p = pos()
        last = p.stop
        for price in (101, 105, 103, 112, 109, 120, 111):
            p.stop = update_stop(p, float(price), 2.0, CFG)
            self.assertGreaterEqual(p.stop, last)
            last = p.stop


class TestRulePriority(unittest.TestCase):
    def test_stop_checked_against_the_low_not_the_close(self):
        """A position that traded through its stop is out, whatever it closed
        at. Checking the close would report 'still holding' on a name your
        broker already sold."""
        signal = evaluate_position(pos(), quote(98.0, low=95.0), as_of=TODAY, config=CFG)
        self.assertEqual((signal.action, signal.reason), (SELL, "stop"))

    def test_target_trims_and_moves_the_stop_to_breakeven(self):
        signal = evaluate_position(pos(), quote(111.0, high=111.0), as_of=TODAY, config=CFG)
        self.assertEqual((signal.action, signal.reason), (TRIM, "target"))
        self.assertAlmostEqual(signal.suggested_stop, 100.0)
        self.assertAlmostEqual(signal.fraction, 0.5)

    def test_target_sells_whole_when_scale_out_disabled(self):
        cfg = ExitConfig(scale_out_at_target=False)
        signal = evaluate_position(pos(), quote(111.0, high=111.0), as_of=TODAY, config=cfg)
        self.assertEqual((signal.action, signal.reason), (SELL, "target"))

    def test_stop_beats_target_in_the_same_bar(self):
        """Intrabar sequence is unknowable; the pessimistic reading is safe."""
        signal = evaluate_position(pos(), quote(105.0, low=95.0, high=115.0),
                                   as_of=TODAY, config=CFG)
        self.assertEqual(signal.reason, "stop")

    def test_already_scaled_out_does_not_trim_again(self):
        p = pos()
        p.scaled_out = True
        signal = evaluate_position(p, quote(111.0, high=111.0), as_of=TODAY, config=CFG)
        self.assertNotEqual(signal.action, TRIM)

    def test_time_stop(self):
        old = pos(entry_date=date(2026, 5, 1))
        signal = evaluate_position(old, quote(101.0), as_of=TODAY, config=CFG)
        self.assertEqual((signal.action, signal.reason), (SELL, "time"))

    def test_time_stop_does_not_preempt_a_stop_hit(self):
        old = pos(entry_date=date(2026, 5, 1))
        signal = evaluate_position(old, quote(97.0, low=95.0), as_of=TODAY, config=CFG)
        self.assertEqual(signal.reason, "stop")

    def test_trend_break_is_only_a_watch(self):
        signal = evaluate_position(pos(), quote(101.0, ma50=105.0), as_of=TODAY, config=CFG)
        self.assertEqual((signal.action, signal.reason), (WATCH, "trend_break"))

    def test_rs_decay_is_only_a_watch(self):
        signal = evaluate_position(pos(), quote(101.0, ma50=99.0, rs_rank=30.0),
                                   as_of=TODAY, config=CFG)
        self.assertEqual((signal.action, signal.reason), (WATCH, "rs_decay"))

    def test_healthy_position_holds(self):
        signal = evaluate_position(pos(), quote(105.0, ma50=99.0, rs_rank=88.0),
                                   as_of=TODAY, config=CFG)
        self.assertEqual(signal.action, HOLD)

    def test_missing_optional_fields_do_not_break_the_stop_rule(self):
        """A stale RS feed must not silently stop the stop-loss from working."""
        signal = evaluate_position(pos(), {"close": 95.0, "low": 95.0},
                                   as_of=TODAY, config=CFG)
        self.assertEqual(signal.action, SELL)


class TestEvaluateBook(unittest.TestCase):
    def test_sorted_by_urgency(self):
        positions = [pos(), Position(symbol="B", entry_date=date(2026, 8, 10),
                                     entry_price=50.0, shares=10, stop=47.0, target=57.5)]
        positions[0].symbol = "A"
        signals = evaluate_book(positions,
                                {"A": quote(105.0, ma50=99.0),
                                 "B": quote(46.0, low=46.0)},
                                as_of=TODAY, config=CFG)
        self.assertEqual(signals[0].symbol, "B")      # SELL sorts first
        self.assertEqual(signals[0].action, SELL)

    def test_missing_quote_becomes_watch_not_silence(self):
        """Silence about a position you hold is the one output this must never
        produce."""
        signals = evaluate_book([pos()], {}, as_of=TODAY, config=CFG)
        self.assertEqual(len(signals), 1)
        self.assertEqual((signals[0].action, signals[0].reason), (WATCH, "no_quote"))

    def test_empty_book(self):
        self.assertEqual(evaluate_book([], {}, as_of=TODAY, config=CFG), [])


class TestConfig(unittest.TestCase):
    def test_loads_from_yaml(self):
        cfg = ExitConfig.from_yaml()
        self.assertEqual(cfg.max_hold_days, 40)
        self.assertTrue(cfg.scale_out_at_target)

    def test_rejects_trail_before_breakeven(self):
        with self.assertRaisesRegex(ValueError, "below breakeven"):
            ExitConfig(breakeven_after_r=2.0, trail_after_r=1.0).validate()

    def test_rejects_bad_scale_fraction(self):
        with self.assertRaisesRegex(ValueError, "strictly between"):
            ExitConfig(scale_out_fraction=1.0).validate()


if __name__ == "__main__":
    unittest.main(verbosity=1)
