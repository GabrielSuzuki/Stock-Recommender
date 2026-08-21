"""Sizing tests. Every expected number is worked by hand in the test name or body."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.sizing import (Portfolio, SizingConfig, apply_caps,  # noqa: E402
                         portfolio_summary, raw_shares, size_candidates,
                         stop_price, target_price)

CFG = SizingConfig(account_equity=100_000, risk_per_trade_pct=1.0,
                   atr_stop_multiple=2.0, target_r_multiple=2.5,
                   max_open_positions=6, max_portfolio_heat_pct=6.0,
                   max_position_pct=25.0, max_same_sector=2,
                   max_participation_pct=0.5, min_shares=1)


def candidate(symbol="AAA", close=100.0, atr=2.0, avg_volume=1_000_000.0,
              sector="Tech", rank=1.0):
    return pd.DataFrame(
        [{"close": close, "atr": atr, "avg_volume": avg_volume,
          "sector": sector, "rank_score": rank, "rs_rank": 90.0}],
        index=pd.Index([symbol], name="symbol"))


def candidates(specs):
    rows, idx = [], []
    for spec in specs:
        idx.append(spec.pop("symbol"))
        rows.append({"close": 100.0, "atr": 2.0, "avg_volume": 1_000_000.0,
                     "sector": "Tech", "rank_score": 1.0, "rs_rank": 90.0, **spec})
    return pd.DataFrame(rows, index=pd.Index(idx, name="symbol"))


class TestSinglePositionMath(unittest.TestCase):
    def test_stop_is_two_atrs_below(self):
        # entry 100, ATR 2, multiple 2 -> stop 96
        self.assertAlmostEqual(stop_price(100.0, 2.0, 2.0), 96.0)

    def test_target_is_r_multiple_of_risk(self):
        # risk 4/share, 2.5R -> 100 + 10 = 110
        self.assertAlmostEqual(target_price(100.0, 96.0, 2.5), 110.0)

    def test_shares_from_risk_budget(self):
        # $1000 risk / $4 per share = 250 shares
        self.assertEqual(raw_shares(1000.0, 100.0, 96.0), 250)

    def test_shares_round_down_never_up(self):
        # $1000 / $3 = 333.33 -> 333, so actual risk is under budget not over
        self.assertEqual(raw_shares(1000.0, 100.0, 97.0), 333)

    def test_zero_or_inverted_risk_returns_zero(self):
        self.assertEqual(raw_shares(1000.0, 100.0, 100.0), 0)
        self.assertEqual(raw_shares(1000.0, 100.0, 101.0), 0)


class TestCaps(unittest.TestCase):
    def test_notional_cap_binds(self):
        # 25% of 100k = $25,000 / $100 = 250 shares max
        shares, cap = apply_caps(500, 100.0, 10_000_000, CFG)
        self.assertEqual(shares, 250)
        self.assertEqual(cap, "notional")

    def test_liquidity_cap_binds(self):
        # 0.5% of 20,000 ADV = 100 shares
        shares, cap = apply_caps(250, 100.0, 20_000, CFG)
        self.assertEqual(shares, 100)
        self.assertEqual(cap, "liquidity")

    def test_liquidity_beats_notional_when_tighter(self):
        shares, cap = apply_caps(1000, 100.0, 10_000, CFG)
        self.assertEqual(shares, 50)        # 0.5% of 10k
        self.assertEqual(cap, "liquidity")

    def test_no_cap_when_neither_binds(self):
        shares, cap = apply_caps(100, 100.0, 10_000_000, CFG)
        self.assertEqual((shares, cap), (100, None))


class TestSizeCandidates(unittest.TestCase):
    def test_full_plan_on_a_clean_candidate(self):
        out = size_candidates(candidate(), Portfolio.empty(), CFG)
        row = out.loc["AAA"]
        self.assertTrue(bool(row["sizable"]))
        self.assertAlmostEqual(row["entry"], 100.0)
        self.assertAlmostEqual(row["stop"], 96.0)
        self.assertAlmostEqual(row["target"], 110.0)
        # risk budget 1% of 100k = $1000 / $4 = 250, but notional caps at 250 too
        self.assertEqual(int(row["shares"]), 250)
        self.assertAlmostEqual(row["risk_dollars"], 1000.0)

    def test_risk_never_exceeds_budget(self):
        for close, atr in [(37.0, 1.3), (412.0, 9.7), (8.5, 0.42), (155.0, 4.1)]:
            with self.subTest(close=close):
                out = size_candidates(candidate(close=close, atr=atr,
                                                avg_volume=50_000_000), Portfolio.empty(), CFG)
                self.assertLessEqual(out.loc["AAA", "risk_dollars"], CFG.risk_dollars + 1e-9)

    def test_missing_atr_is_rejected(self):
        out = size_candidates(candidate(atr=np.nan), Portfolio.empty(), CFG)
        self.assertFalse(bool(out.loc["AAA", "sizable"]))
        self.assertIn("ATR", out.loc["AAA", "sizing_rejection"])

    def test_stop_below_zero_is_rejected(self):
        # $3 stock with $2 ATR: 2x ATR stop lands at -1
        out = size_candidates(candidate(close=3.0, atr=2.0), Portfolio.empty(), CFG)
        self.assertFalse(bool(out.loc["AAA", "sizable"]))
        self.assertIn("zero", out.loc["AAA", "sizing_rejection"])

    def test_position_slots_respected(self):
        specs = [{"symbol": f"S{i}", "sector": f"Sec{i}"} for i in range(10)]
        out = size_candidates(candidates(specs), Portfolio.empty(), CFG)
        self.assertEqual(int(out["sizable"].sum()), CFG.max_open_positions)

    def test_sector_cap_respected_when_sector_known(self):
        specs = [{"symbol": f"S{i}", "sector": "Tech"} for i in range(5)]
        out = size_candidates(candidates(specs), Portfolio.empty(), CFG)
        self.assertEqual(int(out["sizable"].sum()), CFG.max_same_sector)
        self.assertIn("sector", out.loc["S2", "sizing_rejection"])

    def test_unknown_sector_does_not_trigger_the_cap(self):
        """The regression that motivated the fix.

        With no sector feed every candidate falls into one bucket. Enforcing
        the cap there would silently limit the book to max_same_sector and
        report a rejection reason ("sector full") that is not true.
        """
        specs = [{"symbol": f"S{i}", "sector": ""} for i in range(10)]
        out = size_candidates(candidates(specs), Portfolio.empty(), CFG)
        self.assertEqual(int(out["sizable"].sum()), CFG.max_open_positions)
        self.assertEqual(portfolio_summary(out, CFG)["sectors_unknown"],
                         CFG.max_open_positions)

    def test_heat_cap_trims_rather_than_rejects(self):
        cfg = SizingConfig(account_equity=100_000, risk_per_trade_pct=1.0,
                           max_portfolio_heat_pct=1.5, max_same_sector=99,
                           max_participation_pct=100.0)
        specs = [{"symbol": f"S{i}", "sector": f"Sec{i}"} for i in range(4)]
        out = size_candidates(candidates(specs), Portfolio.empty(), cfg)
        total = out.loc[out["sizable"], "risk_dollars"].sum()
        self.assertLessEqual(total, 1500.0 + 1e-6)
        self.assertEqual(out.loc["S1", "binding_cap"], "heat")   # second one trimmed

    def test_existing_portfolio_reduces_capacity(self):
        book = Portfolio(open_risk_dollars=5000.0,
                         positions={"OLD1": 2500.0, "OLD2": 2500.0},
                         sectors={"Tech": 2})
        specs = [{"symbol": f"S{i}", "sector": "Tech"} for i in range(3)]
        out = size_candidates(candidates(specs), book, CFG)
        self.assertEqual(int(out["sizable"].sum()), 0)           # sector already full

    def test_already_held_is_skipped(self):
        book = Portfolio(open_risk_dollars=500.0, positions={"AAA": 500.0})
        out = size_candidates(candidate("AAA"), book, CFG)
        self.assertIn("already", out.loc["AAA", "sizing_rejection"])

    def test_best_ranked_gets_capacity_first(self):
        specs = [{"symbol": "BEST", "sector": "A"}, {"symbol": "OK", "sector": "B"},
                 {"symbol": "MEH", "sector": "C"}]
        cfg = SizingConfig(account_equity=100_000, max_open_positions=1,
                           max_portfolio_heat_pct=6.0, max_same_sector=9)
        out = size_candidates(candidates(specs), Portfolio.empty(), cfg)
        self.assertTrue(bool(out.loc["BEST", "sizable"]))
        self.assertFalse(bool(out.loc["OK", "sizable"]))

    def test_empty_input(self):
        empty = pd.DataFrame(columns=["close", "atr", "avg_volume", "sector"])
        out = size_candidates(empty, Portfolio.empty(), CFG)
        self.assertTrue(out.empty)
        self.assertEqual(portfolio_summary(out, CFG)["positions"], 0)

    def test_summary_arithmetic(self):
        specs = [{"symbol": f"S{i}", "sector": f"Sec{i}"} for i in range(4)]
        out = size_candidates(candidates(specs), Portfolio.empty(), CFG)
        summary = portfolio_summary(out, CFG)
        taken = out[out["sizable"]]
        self.assertEqual(summary["positions"], len(taken))
        self.assertAlmostEqual(summary["total_risk_dollars"],
                               round(taken["risk_dollars"].sum(), 2))
        self.assertAlmostEqual(
            summary["portfolio_heat_pct"],
            round(taken["risk_dollars"].sum() / CFG.account_equity * 100, 2))


class TestConfigValidation(unittest.TestCase):
    def test_loads_from_yaml(self):
        cfg = SizingConfig.from_yaml()
        self.assertEqual(cfg.risk_per_trade_pct, 0.75)
        self.assertEqual(cfg.atr_stop_multiple, 2.0)
        self.assertEqual(cfg.max_open_positions, 6)

    def test_risk_dollars(self):
        self.assertAlmostEqual(
            SizingConfig(account_equity=50_000, risk_per_trade_pct=0.5).risk_dollars, 250.0)

    def test_rejects_absurd_risk(self):
        with self.assertRaisesRegex(ValueError, "outside 0-5"):
            SizingConfig(risk_per_trade_pct=25.0).validate()

    def test_rejects_heat_below_per_trade_risk(self):
        with self.assertRaisesRegex(ValueError, "no trade could ever be taken"):
            SizingConfig(risk_per_trade_pct=2.0, max_portfolio_heat_pct=1.0).validate()

    def test_env_overrides_equity(self):
        import os
        os.environ["SCREENER_EQUITY"] = "77000"
        try:
            self.assertEqual(SizingConfig.from_yaml().account_equity, 77_000.0)
        finally:
            del os.environ["SCREENER_EQUITY"]


if __name__ == "__main__":
    unittest.main(verbosity=1)
