"""Preflight tests — the checker must not itself be the thing that breaks."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.preflight import (BLOCK, OK, SKIP, WARN,  # noqa: E402
                                Preflight, apply_volume_scale)


class TestStatusAggregation(unittest.TestCase):
    def test_blocks_make_it_not_ready(self):
        pf = Preflight()
        pf.add("a", OK)
        pf.add("b", BLOCK, "broken")
        self.assertEqual(pf.summary(), 1)

    def test_warnings_alone_still_ready(self):
        """A degraded run is still a run. Only a BLOCK stops the launch."""
        pf = Preflight()
        pf.add("a", OK)
        pf.add("b", WARN, "degraded")
        self.assertEqual(pf.summary(), 0)

    def test_skips_are_not_failures(self):
        pf = Preflight()
        pf.add("a", SKIP, "no credentials")
        self.assertEqual(pf.summary(), 0)

    def test_empty_is_ready(self):
        self.assertEqual(Preflight().summary(), 0)


class TestEnvChecks(unittest.TestCase):
    KEYS = ("ALPACA_API_KEY_ID", "ALPACA_API_SECRET_KEY", "TELEGRAM_BOT_TOKEN",
            "TELEGRAM_CHAT_ID", "FINNHUB_API_KEY", "ANTHROPIC_API_KEY",
            "MARKETAUX_API_KEY", "SEC_USER_AGENT")

    def setUp(self):
        self.saved = {k: os.environ.pop(k, None) for k in self.KEYS}

    def tearDown(self):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_missing_alpaca_keys_block(self):
        pf = Preflight()
        pf.check_env()
        blocked = {c.name for c in pf.checks if c.status == BLOCK}
        self.assertIn("ALPACA_API_KEY_ID", blocked)
        self.assertIn("ALPACA_API_SECRET_KEY", blocked)

    def test_missing_optional_keys_only_warn(self):
        pf = Preflight()
        pf.check_env()
        warned = {c.name for c in pf.checks if c.status == WARN}
        self.assertIn("FINNHUB_API_KEY", warned)
        self.assertNotIn("FINNHUB_API_KEY",
                         {c.name for c in pf.checks if c.status == BLOCK})

    def test_present_keys_pass(self):
        for key in self.KEYS:
            os.environ[key] = "x"
        os.environ["SEC_USER_AGENT"] = "Test User test@example.com"
        pf = Preflight()
        pf.check_env()
        self.assertEqual([c for c in pf.checks if c.status in (BLOCK, WARN)], [])

    def test_malformed_sec_user_agent_warns(self):
        os.environ["SEC_USER_AGENT"] = "justaname"
        pf = Preflight()
        pf.check_env()
        agent = next(c for c in pf.checks if c.name == "SEC_USER_AGENT")
        self.assertEqual(agent.status, WARN)
        self.assertIn("EDGAR", agent.fix)

    def test_every_failure_carries_a_fix(self):
        """A check that says something is broken without saying what to do
        about it is worse than no check."""
        pf = Preflight()
        pf.check_env()
        for check in pf.checks:
            if check.status in (BLOCK, WARN):
                self.assertTrue(check.fix, f"{check.name} has no fix text")


class TestVolumeScaleWriter(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.cwd = os.getcwd()
        os.chdir(self.dir.name)
        Path("config").mkdir()
        Path("config/screen.yaml").write_text(
            "universe:\n  source: sp500\n  min_price: 5.0\n")

    def tearDown(self):
        os.chdir(self.cwd)
        self.dir.cleanup()

    def test_inserts_when_absent(self):
        apply_volume_scale(28.0)
        raw = Path("config/screen.yaml").read_text()
        self.assertIn("volume_scale: 28.0", raw)
        self.assertIn("min_price: 5.0", raw)      # existing keys survive

    def test_updates_when_present(self):
        apply_volume_scale(28.0)
        apply_volume_scale(31.5)
        raw = Path("config/screen.yaml").read_text()
        self.assertIn("volume_scale: 31.5", raw)
        self.assertNotIn("28.0", raw)

    def test_result_is_still_valid_yaml(self):
        import yaml
        apply_volume_scale(25.0)
        parsed = yaml.safe_load(Path("config/screen.yaml").read_text())
        self.assertEqual(parsed["universe"]["volume_scale"], 25.0)
        self.assertEqual(parsed["universe"]["min_price"], 5.0)


class TestDryRunIsolation(unittest.TestCase):
    """The regression that motivated the subprocess.

    `check_pipeline` ran Stage A offline in-process, which wrote synthetic
    candidates over the real `candidates.json`. The next Stage B then briefed
    on SYN014 and SYN020 -- entirely plausible-looking output about stocks that
    do not exist. A check that destroys the thing it checks is worse than none.
    """

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.saved = {k: os.environ.get(k)
                      for k in ("SCREENER_DATA", "SCREENER_JOURNAL", "SCREENER_LOGS")}
        for k in self.saved:
            os.environ[k] = self.dir.name
        for module in [m for m in sys.modules if m.startswith(("pipeline", "mcp"))]:
            del sys.modules[module]

    def tearDown(self):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.dir.cleanup()

    def test_dry_run_does_not_touch_candidates_json(self):
        import json
        from pipeline import paths
        from pipeline.preflight import Preflight

        paths.CANDIDATES.parent.mkdir(parents=True, exist_ok=True)
        sentinel = {"run": {"as_of": "2026-08-20"},
                    "candidates": [{"symbol": "REAL_TICKER"}]}
        paths.CANDIDATES.write_text(json.dumps(sentinel))

        pf = Preflight()
        pf.check_pipeline()

        after = json.loads(paths.CANDIDATES.read_text())
        self.assertEqual(after["candidates"][0]["symbol"], "REAL_TICKER",
                         "preflight overwrote live candidates.json")

    def test_dry_run_reports_both_stages(self):
        from pipeline.preflight import Preflight
        pf = Preflight()
        pf.check_pipeline()
        names = {c.name for c in pf.checks}
        self.assertIn("stage A", names)
        self.assertIn("stage B", names)


class TestEnvironmentChecks(unittest.TestCase):
    def test_core_imports_are_present(self):
        pf = Preflight()
        pf.check_environment()
        blocked = [c for c in pf.checks if c.status == BLOCK]
        self.assertEqual(blocked, [], f"missing core dependency: {blocked}")

    def test_non_pacific_timezone_warns(self):
        saved = os.environ.get("TZ")
        os.environ["TZ"] = "UTC"
        try:
            pf = Preflight()
            pf.check_environment()
            tz = next(c for c in pf.checks if c.name == "timezone")
            self.assertEqual(tz.status, WARN)
            self.assertIn("timedatectl", tz.fix)
        finally:
            if saved is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = saved


if __name__ == "__main__":
    unittest.main(verbosity=1)
