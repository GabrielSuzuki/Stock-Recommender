"""Regime tests. The off-switch must be provably reachable and provably safe."""
from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import synthetic as syn                                    # noqa: E402
from core.regime import (NEUTRAL, RISK_OFF, RISK_ON, RegimeConfig,   # noqa: E402
                         assess, breadth_above_ma, realized_volatility)

CFG = RegimeConfig()


class TestRealizedVolatility(unittest.TestCase):
    def test_flat_series_is_zero(self):
        self.assertAlmostEqual(realized_volatility(pd.Series([100.0] * 60)), 0.0)

    def test_hand_computed(self):
        # Alternating +1%/-1% log-ish moves: daily sd is ~1%, annualized
        # 1% * sqrt(252) ~= 15.9%
        closes = [100.0]
        for i in range(60):
            closes.append(closes[-1] * (1.01 if i % 2 == 0 else 1 / 1.01))
        vol = realized_volatility(pd.Series(closes))
        self.assertAlmostEqual(vol, math.log(1.01) * math.sqrt(252) * 100, delta=0.6)

    def test_short_series_is_nan(self):
        self.assertTrue(np.isnan(realized_volatility(pd.Series([1.0, 2.0, 3.0]))))

    def test_higher_dispersion_gives_higher_vol(self):
        rng = np.random.default_rng(0)
        calm = pd.Series(100 * np.cumprod(1 + rng.normal(0, 0.004, 100)))
        wild = pd.Series(100 * np.cumprod(1 + rng.normal(0, 0.030, 100)))
        self.assertLess(realized_volatility(calm), realized_volatility(wild))


class TestBreadth(unittest.TestCase):
    def test_all_uptrends_is_100(self):
        bars = {f"U{i}": syn.stage2(n=300, seed=i) for i in range(5)}
        self.assertAlmostEqual(breadth_above_ma(bars), 100.0)

    def test_all_downtrends_is_zero(self):
        bars = {f"D{i}": syn.downtrend(n=300) for i in range(5)}
        self.assertAlmostEqual(breadth_above_ma(bars), 0.0)

    def test_mixed_is_proportional(self):
        bars = {f"U{i}": syn.stage2(n=300, seed=i) for i in range(3)}
        bars.update({f"D{i}": syn.downtrend(n=300) for i in range(1)})
        self.assertAlmostEqual(breadth_above_ma(bars), 75.0)

    def test_short_histories_excluded_not_counted_as_false(self):
        bars = {"GOOD": syn.stage2(n=300), "SHORT": syn.stage2(n=50)}
        self.assertAlmostEqual(breadth_above_ma(bars), 100.0)

    def test_empty_universe_is_nan(self):
        self.assertTrue(np.isnan(breadth_above_ma({})))


class TestVerdict(unittest.TestCase):
    def setUp(self):
        self.healthy = {f"U{i}": syn.stage2(n=320, seed=i, noise=0.004)
                        for i in range(10)}
        self.bench_up = syn.stage2(n=320, annual_growth=0.15, noise=0.003, seed=99)

    def test_healthy_tape_is_risk_on(self):
        reading = assess(self.healthy, benchmark_bars=self.bench_up, config=CFG)
        self.assertEqual(reading.verdict, RISK_ON)
        self.assertTrue(reading.tradeable)
        self.assertFalse(reading.degraded)

    def test_benchmark_below_50ma_is_risk_off(self):
        reading = assess(self.healthy, benchmark_bars=syn.downtrend(n=320), config=CFG)
        self.assertEqual(reading.verdict, RISK_OFF)
        self.assertFalse(reading.tradeable)
        self.assertTrue(any("50-day" in r for r in reading.reasons))

    def test_poor_breadth_is_risk_off(self):
        bars = {f"D{i}": syn.downtrend(n=320) for i in range(9)}
        bars["U0"] = syn.stage2(n=320)
        reading = assess(bars, benchmark_bars=self.bench_up, config=CFG)
        self.assertEqual(reading.verdict, RISK_OFF)
        self.assertTrue(any("200-day" in r for r in reading.reasons))

    def test_high_volatility_is_risk_off(self):
        rng = np.random.default_rng(3)
        # Rising but violently: keeps the MAs intact so vol is the only trigger.
        closes = 100 * np.cumprod(1 + rng.normal(0.0025, 0.030, 320))
        reading = assess(self.healthy, benchmark_bars=syn.make_bars(closes), config=CFG)
        self.assertEqual(reading.verdict, RISK_OFF)
        self.assertTrue(any("vol" in r for r in reading.reasons))

    def test_missing_benchmark_degrades_to_neutral_never_risk_on(self):
        reading = assess(self.healthy, benchmark_bars=None, config=CFG)
        self.assertEqual(reading.verdict, NEUTRAL)
        self.assertTrue(reading.degraded)
        self.assertTrue(reading.tradeable)

    def test_empty_universe_degrades(self):
        reading = assess({}, benchmark_bars=self.bench_up, config=CFG)
        self.assertIn(reading.verdict, (NEUTRAL, RISK_OFF))
        self.assertNotEqual(reading.verdict, RISK_ON)

    def test_degraded_is_never_risk_on(self):
        """The invariant that matters: if we cannot see the tape, we do not get
        to be confident about it."""
        for bench in (None, pd.DataFrame()):
            with self.subTest(bench=type(bench).__name__):
                reading = assess(self.healthy, benchmark_bars=bench, config=CFG)
                self.assertNotEqual(reading.verdict, RISK_ON)

    def test_risk_multiplier_matches_verdict(self):
        self.assertEqual(assess(self.healthy, self.bench_up, CFG).risk_multiplier, 1.0)

    def test_reading_serialises(self):
        d = assess(self.healthy, self.bench_up, CFG).to_dict()
        for key in ("verdict", "reasons", "metrics", "degraded", "tradeable"):
            self.assertIn(key, d)


class TestConfigValidation(unittest.TestCase):
    def test_loads_from_yaml(self):
        cfg = RegimeConfig.from_yaml()
        self.assertEqual(cfg.benchmark, "SPY")
        self.assertEqual(cfg.off_spy_below_ma, 50)

    def test_rejects_unreachable_risk_off_via_breadth(self):
        """If the NEUTRAL band is stricter than the RISK_OFF one, RISK_OFF can
        never fire -- a broken off-switch that looks like a working one."""
        with self.assertRaisesRegex(ValueError, "unreachable"):
            RegimeConfig(off_breadth_below=50.0, neutral_breadth_below=40.0).validate()

    def test_rejects_unreachable_risk_off_via_vol(self):
        with self.assertRaisesRegex(ValueError, "unreachable"):
            RegimeConfig(off_vol_above=18.0, neutral_vol_above=25.0).validate()

    def test_rejects_bad_multiplier(self):
        with self.assertRaisesRegex(ValueError, "between 0 and 1"):
            RegimeConfig(neutral_risk_multiplier=1.7).validate()


class TestApplyRegime(unittest.TestCase):
    def test_neutral_halves_risk_and_caps_positions(self):
        from core.regime import RegimeReading, apply_regime
        from core.sizing import SizingConfig
        base = SizingConfig(risk_per_trade_pct=1.0, max_open_positions=6)
        out = apply_regime(RegimeReading(verdict=NEUTRAL), base, CFG)
        self.assertAlmostEqual(out.risk_per_trade_pct, 0.5)
        self.assertEqual(out.max_open_positions, 3)

    def test_risk_on_is_unchanged(self):
        from core.regime import RegimeReading, apply_regime
        from core.sizing import SizingConfig
        base = SizingConfig(risk_per_trade_pct=1.0, max_open_positions=6)
        self.assertIs(apply_regime(RegimeReading(verdict=RISK_ON), base, CFG), base)


if __name__ == "__main__":
    unittest.main(verbosity=1)
