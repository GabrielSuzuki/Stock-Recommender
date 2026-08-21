import sys, os, unittest
from pathlib import Path
os.environ.setdefault("SCREENER_DATA", "/tmp/screener-data")
sys.path.insert(0, "/root/stock-recommender")
from pipeline.llm import agent_for, ROLE_TIERS, _namespace

class T(unittest.TestCase):
    def test_every_role_builds(self):
        for role in ROLE_TIERS:
            a = agent_for(role)
            self.assertIsNotNone(a.router)
            self.assertIsNotNone(a.ledger)

    def test_unknown_role_rejected(self):
        with self.assertRaises(ValueError):
            agent_for("nope")

    def test_namespace_is_date_scoped(self):
        from datetime import date
        self.assertTrue(_namespace("catalyst-analyst").endswith(date.today().isoformat()))
        self.assertNotEqual(_namespace("catalyst-analyst"), _namespace("thesis-writer"))

    def test_semantic_cache_off_for_thesis_writer(self):
        # thesis-writer must never be served a cached answer
        self.assertFalse(agent_for("thesis-writer").semantic.enabled)
        self.assertFalse(agent_for("market-regime").semantic.enabled)

    def test_semantic_cache_on_for_catalyst(self):
        self.assertTrue(agent_for("catalyst-analyst").semantic.enabled)

    def test_disabled_by_env(self):
        os.environ["TOKENWISE_ENABLED"] = "0"
        try:
            a = agent_for("catalyst-analyst")
            self.assertFalse(a.semantic.enabled)
            self.assertFalse(a.cache_optimizer.enabled)
            self.assertFalse(a.compactor.enabled)
        finally:
            os.environ["TOKENWISE_ENABLED"] = "1"

if __name__ == "__main__":
    unittest.main(verbosity=2)
