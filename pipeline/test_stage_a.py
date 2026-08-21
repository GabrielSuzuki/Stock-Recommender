"""Stage A contract tests: does candidates.json say what Stage B needs?"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class TestStageAOffline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.TemporaryDirectory()
        os.environ["SCREENER_DATA"] = cls.dir.name
        os.environ["SCREENER_JOURNAL"] = cls.dir.name
        os.environ["SCREENER_LOGS"] = cls.dir.name
        for module in [m for m in sys.modules if m.startswith(("pipeline", "mcp"))]:
            del sys.modules[module]
        from pipeline.stage_a_nightly import run
        cls.payload = run(offline=True, max_symbols=25)
        cls.path = Path(cls.dir.name) / "candidates.json"

    @classmethod
    def tearDownClass(cls):
        cls.dir.cleanup()
        for key in ("SCREENER_DATA", "SCREENER_JOURNAL", "SCREENER_LOGS"):
            os.environ.pop(key, None)

    def test_file_written_and_parses(self):
        self.assertTrue(self.path.exists())
        json.loads(self.path.read_text())

    def test_schema_version_present(self):
        self.assertEqual(self.payload["schema_version"], 1)

    def test_top_level_contract(self):
        for key in ("run", "data", "screen", "funnel", "config",
                    "portfolio", "candidates"):
            self.assertIn(key, self.payload)

    def test_run_block(self):
        run = self.payload["run"]
        self.assertEqual(run["stage"], "A")
        self.assertIn("as_of", run)
        self.assertIn("generated_at", run)

    def test_candidate_shape(self):
        for c in self.payload["candidates"]:
            self.assertIn("symbol", c)
            self.assertIn("plan", c)
            for key in ("sizable", "entry", "stop", "target", "shares",
                        "risk_dollars", "rejection"):
                self.assertIn(key, c["plan"])

    def test_no_nan_leaks_into_json(self):
        """NaN is not valid JSON. Python's encoder emits a bare NaN token that
        many parsers reject, so every float goes through a converter."""
        raw = self.path.read_text()
        self.assertNotIn("NaN", raw)
        self.assertNotIn("Infinity", raw)

    def test_sized_plans_are_internally_consistent(self):
        for c in self.payload["candidates"]:
            plan = c["plan"]
            if not plan["sizable"]:
                continue
            with self.subTest(symbol=c["symbol"]):
                self.assertLess(plan["stop"], plan["entry"])
                self.assertGreater(plan["target"], plan["entry"])
                self.assertGreater(plan["shares"], 0)
                expected = plan["shares"] * (plan["entry"] - plan["stop"])
                self.assertAlmostEqual(plan["risk_dollars"], expected, places=2)

    def test_rejected_candidates_are_kept_with_a_reason(self):
        rejected = [c for c in self.payload["candidates"] if not c["plan"]["sizable"]]
        for c in rejected:
            self.assertIsNotNone(c["plan"]["rejection"], c["symbol"])

    def test_funnel_included_so_an_empty_day_is_explainable(self):
        funnel = self.payload["funnel"]
        self.assertTrue(funnel)
        for row in funnel:
            for key in ("condition", "label", "eliminated", "surviving"):
                self.assertIn(key, row)

    def test_portfolio_heat_within_cap(self):
        from core.sizing import SizingConfig
        cfg = SizingConfig.from_yaml()
        self.assertLessEqual(self.payload["portfolio"]["portfolio_heat_pct"],
                             cfg.max_portfolio_heat_pct + 0.01)

    def test_candidate_count_within_cap(self):
        self.assertLessEqual(len(self.payload["candidates"]),
                             self.payload["config"]["max_candidates"])

    def test_heartbeat_written(self):
        beat = json.loads((Path(self.dir.name) / "heartbeat.json").read_text())
        self.assertEqual(beat["stage"], "A")
        self.assertIn("completed_at", beat)

    def test_data_failures_reported_separately(self):
        self.assertIn("data_failures", self.payload)
        self.assertIsInstance(self.payload["data_failures"], dict)


class TestWatchdog(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        os.environ["SCREENER_DATA"] = self.dir.name
        for module in [m for m in sys.modules if m.startswith("pipeline")]:
            del sys.modules[module]

    def tearDown(self):
        self.dir.cleanup()
        os.environ.pop("SCREENER_DATA", None)

    def test_missing_everything_is_reported(self):
        from pipeline.watchdog import check
        problems = check()
        self.assertTrue(any("heartbeat" in p for p in problems))
        self.assertTrue(any("candidates.json missing" in p for p in problems))

    def test_stale_candidates_detected(self):
        from datetime import datetime, timedelta
        from pipeline import paths
        from pipeline.watchdog import check
        paths.CANDIDATES.parent.mkdir(parents=True, exist_ok=True)
        paths.CANDIDATES.write_text(json.dumps(
            {"run": {"as_of": "2020-01-01"}}))
        (paths.DATA_DIR / "heartbeat_b.json").write_text(json.dumps(
            {"completed_at": datetime.now().astimezone().isoformat()}))
        problems = check()
        self.assertTrue(any("stale prices" in p for p in problems), problems)

    def test_clean_state_reports_nothing(self):
        from datetime import date, datetime
        from pipeline import paths
        from pipeline.watchdog import check
        paths.CANDIDATES.parent.mkdir(parents=True, exist_ok=True)
        paths.CANDIDATES.write_text(json.dumps({"run": {"as_of": date.today().isoformat()}}))
        (paths.DATA_DIR / "heartbeat_b.json").write_text(json.dumps(
            {"completed_at": datetime.now().astimezone().isoformat()}))
        self.assertEqual(check(), [])


class TestNotifyFailure(unittest.TestCase):
    def test_silent_on_success(self):
        from pipeline.notify_failure import main
        os.environ["SERVICE_RESULT"] = "success"
        try:
            self.assertEqual(main(["stage_b"]), 0)
        finally:
            os.environ.pop("SERVICE_RESULT", None)

    def test_silent_when_unset(self):
        from pipeline.notify_failure import main
        os.environ.pop("SERVICE_RESULT", None)
        self.assertEqual(main(["stage_b"]), 0)


if __name__ == "__main__":
    unittest.main(verbosity=1)
