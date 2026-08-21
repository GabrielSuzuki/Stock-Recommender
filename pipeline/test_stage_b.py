"""Agent harness, brief rendering, journal and Stage B orchestration."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.agents.base import (AgentError, JsonAgent,  # noqa: E402
                                  OfflineModel, Schema, extract_json)

TODAY = date(2026, 8, 19)


class FakeClient:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def ask(self, prompt, system=None, **_):
        self.calls.append(prompt)
        return self.responses.pop(0) if self.responses else "{}"


class TestExtractJson(unittest.TestCase):
    def test_bare_object(self):
        self.assertEqual(extract_json('{"a": 1}'), {"a": 1})

    def test_fenced(self):
        self.assertEqual(extract_json('```json\n{"a": 1}\n```'), {"a": 1})

    def test_fenced_without_language(self):
        self.assertEqual(extract_json('```\n{"a": 1}\n```'), {"a": 1})

    def test_with_preamble_and_trailer(self):
        text = 'Here you go:\n{"a": 1}\nLet me know if you need changes.'
        self.assertEqual(extract_json(text), {"a": 1})

    def test_array(self):
        self.assertEqual(extract_json('[{"a": 1}, {"a": 2}]'), [{"a": 1}, {"a": 2}])

    def test_nested_braces_in_prose(self):
        self.assertEqual(extract_json('note {x}\n{"a": {"b": 2}}'), {"a": {"b": 2}})

    def test_empty_raises(self):
        for bad in ("", "   ", None):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                extract_json(bad)

    def test_no_json_raises(self):
        with self.assertRaises(ValueError):
            extract_json("I am afraid I cannot do that.")


class TestSchema(unittest.TestCase):
    S = Schema(required={"symbol": str, "quality": int}, optional={"notes": list})

    def test_valid(self):
        self.assertEqual(self.S.validate({"symbol": "A", "quality": 3}), [])

    def test_missing_key(self):
        problems = self.S.validate({"symbol": "A"})
        self.assertTrue(any("quality" in p and "missing" in p for p in problems))

    def test_wrong_type(self):
        problems = self.S.validate({"symbol": "A", "quality": "three"})
        self.assertTrue(any("expected number" in p for p in problems))

    def test_optional_absent_is_fine(self):
        self.assertEqual(self.S.validate({"symbol": "A", "quality": 1}), [])

    def test_optional_null_is_fine(self):
        self.assertEqual(self.S.validate({"symbol": "A", "quality": 1, "notes": None}), [])

    def test_array_schema(self):
        arr = Schema(array_of=Schema(required={"symbol": str}))
        self.assertEqual(arr.validate([{"symbol": "A"}, {"symbol": "B"}]), [])
        self.assertTrue(arr.validate({"symbol": "A"}))      # object, not array
        self.assertTrue(arr.validate([{"nope": 1}]))

    def test_describe_is_short_enough_for_a_repair_prompt(self):
        self.assertLess(len(self.S.describe()), 200)
        self.assertIn("symbol", self.S.describe())


class TestJsonAgent(unittest.TestCase):
    S = Schema(required={"symbol": str, "quality": int})

    def test_happy_path(self):
        agent = JsonAgent("catalyst-analyst",
                          client=FakeClient('{"symbol": "A", "quality": 4}'))
        self.assertEqual(agent.ask("go", self.S)["quality"], 4)

    def test_repairs_unparseable_response(self):
        client = FakeClient("I cannot comply", '{"symbol": "A", "quality": 2}')
        self.assertEqual(JsonAgent("x", client=client).ask("go", self.S)["quality"], 2)
        self.assertEqual(len(client.calls), 2)
        self.assertIn("could not be used", client.calls[1])

    def test_repairs_schema_violation(self):
        client = FakeClient('{"symbol": "A"}', '{"symbol": "A", "quality": 1}')
        JsonAgent("x", client=client).ask("go", self.S)
        self.assertIn("missing", client.calls[1])

    def test_gives_up_after_one_repair(self):
        client = FakeClient("nope", "still nope")
        with self.assertRaises(AgentError):
            JsonAgent("x", client=client).ask("go", self.S)
        self.assertEqual(len(client.calls), 2)

    def test_repair_prompt_carries_the_schema(self):
        client = FakeClient("nope", '{"symbol": "A", "quality": 1}')
        JsonAgent("x", client=client).ask("go", self.S)
        self.assertIn("quality", client.calls[1])

    def test_no_schema_means_parse_only(self):
        self.assertEqual(JsonAgent("x", client=FakeClient('{"anything": true}')).ask("go"),
                         {"anything": True})


class TestOfflineModel(unittest.TestCase):
    def test_catalyst_response_matches_the_real_schema(self):
        from pipeline.agents.catalyst import SCHEMA
        raw = json.loads(OfflineModel().ask("CATALYST CHECK\nSYMBOL: AAPL\n"))
        self.assertEqual(SCHEMA.validate(raw), [])

    def test_thesis_response_matches_the_real_schema(self):
        from pipeline.agents.thesis import SCHEMA
        prompt = 'WRITE THESES\n[{"symbol": "AAA"}, {"symbol": "BBB"}]'
        self.assertEqual(SCHEMA.validate(json.loads(OfflineModel().ask(prompt))), [])

    def test_editor_response_matches_the_real_schema(self):
        from pipeline.agents.editor import SCHEMA
        raw = json.loads(OfflineModel().ask("COMPOSE BRIEF — market line"))
        self.assertEqual(SCHEMA.validate(raw), [])

    def test_thesis_stub_ignores_the_template_placeholder(self):
        """The output template contains "symbol": "...", which a naive regex
        picks up and then reports as an unknown ticker."""
        prompt = 'WRITE THESES\n[{"symbol": "AAA"}]\nReturn [{"symbol": "...", ...}]'
        picks = json.loads(OfflineModel().ask(prompt))
        self.assertNotIn("...", [p["symbol"] for p in picks])


class TestTrim(unittest.TestCase):
    from pipeline.agents.thesis import _trim
    trim = staticmethod(_trim)

    def test_short_text_untouched(self):
        self.assertEqual(self.trim("Short.", 100), "Short.")

    def test_never_ends_mid_word(self):
        """The first live brief ended an invalidation with '...a sector
        unwind, not'. A reader cannot tell a truncated sentence from a
        garbled one."""
        text = ("Loss of the shelf on expanding volume, or crude margin weakness "
                "that breaks the complex entirely — if the group rolls over then "
                "this is a sector unwind rather than a single-name failure.")
        for limit in (60, 100, 150, 200):
            with self.subTest(limit=limit):
                out = self.trim(text, limit)
                self.assertLessEqual(len(out), limit + 1)
                tail = out.rstrip("…").rstrip()
                self.assertTrue(
                    tail.endswith((".", ";")) or text.startswith(tail),
                    f"cut mid-word: {out!r}")
                if not tail.endswith((".", ";")):
                    self.assertFalse(
                        text[len(tail):len(tail) + 1].isalnum(),
                        f"cut inside a word: {out!r}")

    def test_prefers_a_sentence_boundary_when_it_keeps_enough(self):
        """A sentence break is preferred only if it retains most of the budget.

        Cutting at the first full stop when that stop is near the start would
        throw away half the content to gain a period. Below that threshold,
        more text with a visible ellipsis is the better trade.
        """
        text = ("A reasonably long opening sentence that fills the budget. "
                "Then a second one.")
        self.assertEqual(self.trim(text, 60),
                         "A reasonably long opening sentence that fills the budget.")

    def test_falls_back_to_a_word_boundary_with_an_ellipsis(self):
        text = "First one. Second sentence that is much longer than the cap."
        out = self.trim(text, 40)
        self.assertTrue(out.endswith("…"))
        self.assertGreater(len(out), 30)      # kept more than just "First one."

    def test_collapses_whitespace(self):
        self.assertEqual(self.trim("a\n\n  b   c", 100), "a b c")


class TestThesisPrompt(unittest.TestCase):
    """The first live brief rationalised a heat-capped position as a
    deliberate sizing choice: "the right size for a story described as
    moderating rather than accelerating". The size was mechanical -- the heat
    budget ran out. Attributing intent to a number the model did not choose is
    a quiet form of confabulation, and the fix is to tell it which cap bound.
    """

    def setUp(self):
        from datetime import date

        from pipeline.agents.thesis import SYSTEM, _prompt
        self.system = SYSTEM
        self.prompt = _prompt(
            [{"symbol": "TGT", "sector": "Consumer", "rs_rank": 88.0,
              "plan": {"entry": 159.14, "stop": 150.25, "target": 181.36,
                       "shares": 11, "risk_dollars": 97.79, "binding_cap": "heat"}}],
            {"verdict": "RISK_ON", "reasons": [], "metrics": {}},
            date(2026, 8, 20), 5)

    def test_binding_cap_is_surfaced(self):
        self.assertIn("size_limited_by", self.prompt)
        self.assertIn("heat", self.prompt)

    def test_system_prompt_forbids_reading_conviction_from_size(self):
        self.assertIn("NO INFORMATION ABOUT CONVICTION", self.system)
        self.assertIn("conviction field", self.system)

    def test_no_binding_cap_serialises_as_null(self):
        from datetime import date

        from pipeline.agents.thesis import _prompt
        out = _prompt([{"symbol": "AAA", "plan": {"binding_cap": None}}],
                      {"verdict": "RISK_ON", "reasons": [], "metrics": {}},
                      date(2026, 8, 20), 5)
        self.assertIn('"size_limited_by": null', out)


class TestRender(unittest.TestCase):
    from pipeline.agents import editor

    PICK = {"symbol": "AAPL", "conviction": 4,
            "thesis": "Base breakout on 3x volume (sector RS top-3).",
            "invalidation": "closes below 176.20",
            "plan": {"entry": 182.5, "stop": 176.2, "target": 198.25,
                     "shares": 29, "risk_dollars": 182.7}}
    SUMMARY = {"portfolio_heat_pct": 3.2, "evaluated": 480, "disqualified": 4}

    def render(self, picks, regime, **kw):
        return self.editor.render(picks, regime, kw.pop("summary", self.SUMMARY),
                                  TODAY, kw.pop("line", "Tape is constructive."), **kw)

    def test_risk_off_shows_no_picks(self):
        text = self.render([self.PICK], {"verdict": "RISK_OFF",
                                         "reasons": ["SPY below its 50-day"]})
        self.assertIn("No new entries", text)
        self.assertNotIn("AAPL", text)

    def test_empty_picks_message(self):
        text = self.render([], {"verdict": "RISK_ON", "reasons": []})
        self.assertIn("Nothing passed the screen", text)

    def test_pick_rendered_with_escaped_numbers(self):
        text = self.render([self.PICK], {"verdict": "RISK_ON", "reasons": []})
        self.assertIn("*AAPL*", text)
        self.assertIn("182\\.50", text)
        self.assertIn("$183", text)

    def test_dynamic_prose_is_escaped(self):
        text = self.render([self.PICK], {"verdict": "RISK_ON", "reasons": []})
        self.assertIn("\\(sector RS top\\-3\\)", text)

    def test_fits_one_telegram_message(self):
        picks = [dict(self.PICK, symbol=f"TCK{i}") for i in range(5)]
        self.assertLess(len(self.render(picks, {"verdict": "RISK_ON", "reasons": []})),
                        4096)

    def test_overlong_input_drops_picks_rather_than_splitting(self):
        fat = dict(self.PICK, thesis="word " * 300)
        picks = [dict(fat, symbol=f"T{i}") for i in range(12)]
        text = self.render(picks, {"verdict": "RISK_ON", "reasons": []})
        self.assertLess(len(text), 4096)

    def test_footer_carries_heat_and_disqualified(self):
        text = self.render([self.PICK], {"verdict": "RISK_ON", "reasons": []})
        self.assertIn("heat 3", text)
        self.assertIn("4 disqualified", text)

    def test_degraded_run_is_flagged(self):
        text = self.render([self.PICK], {"verdict": "NEUTRAL", "reasons": []},
                           summary={**self.SUMMARY, "degraded": True})
        self.assertIn("DEGRADED", text)

    def test_reply_hint_always_present(self):
        for verdict in ("RISK_ON", "NEUTRAL", "RISK_OFF"):
            with self.subTest(verdict=verdict):
                self.assertIn("TOOK TICKER",
                              self.render([self.PICK], {"verdict": verdict, "reasons": []}))

    def test_market_line_falls_back_to_rule_text(self):
        regime = {"verdict": "RISK_OFF", "reasons": ["SPY below its 50-day"],
                  "metrics": {}}
        line = self.editor.market_line(regime, 0,
                                       agent=JsonAgent("x", client=FakeClient("garbage",
                                                                              "garbage")))
        self.assertEqual(line, "SPY below its 50-day")


class TestJournal(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        from mcp.journal import Journal
        self.journal = Journal(self.dir.name)

    def tearDown(self):
        self.dir.cleanup()

    def test_brief_roundtrip(self):
        self.journal.record_brief({"as_of": "2026-08-19", "regime": "RISK_ON",
                                   "picks": [{"symbol": "AAPL"}]})
        briefs = self.journal.read_briefs()
        self.assertEqual(len(briefs), 1)
        self.assertEqual(briefs[0]["regime"], "RISK_ON")
        self.assertIn("logged_at", briefs[0])

    def test_empty_days_are_recorded(self):
        self.journal.record_brief({"as_of": "2026-08-19", "regime": "RISK_OFF",
                                   "picks": []})
        self.assertEqual(self.journal.stats()["regime_days"], {"RISK_OFF": 1})

    def test_corrupt_line_does_not_lose_the_journal(self):
        self.journal.record_brief({"as_of": "2026-08-18", "regime": "RISK_ON"})
        with self.journal.briefs.open("a") as fh:
            fh.write("{not json\n")
        self.journal.record_brief({"as_of": "2026-08-19", "regime": "NEUTRAL"})
        self.assertEqual(len(self.journal.read_briefs()), 2)

    def test_since_filter(self):
        self.journal.record_brief({"as_of": "2026-01-01", "regime": "RISK_ON"})
        self.journal.record_brief({"as_of": "2026-08-19", "regime": "RISK_ON"})
        self.assertEqual(len(self.journal.read_briefs(since=date(2026, 6, 1))), 1)

    def test_stats(self):
        self.journal.record_brief({"as_of": "2026-08-19", "regime": "RISK_ON",
                                   "picks": [{"symbol": "A"}, {"symbol": "B"}]})
        self.journal.record_fill({"symbol": "A", "quantity": 10, "price": 5.0})
        stats = self.journal.stats()
        self.assertEqual((stats["briefs"], stats["fills"], stats["names_recommended"]),
                         (1, 1, 2))


class TestFillParsing(unittest.TestCase):
    # staticmethod, because a plain function assigned as a class attribute
    # becomes a bound method and swallows the first argument as `self`.
    from mcp.journal import parse_fill_reply
    parse = staticmethod(parse_fill_reply)

    def test_canonical_form(self):
        fill = self.parse("TOOK AAPL 100 @ 182.50")
        self.assertEqual((fill.symbol, fill.quantity, fill.price, fill.side),
                         ("AAPL", 100, 182.50, "buy"))

    def test_variants(self):
        for text in ("bought NVDA 25 at 121", "TOOK  nvda  25  @  121",
                     "SOLD NVDA 25 @ $121.00", "stopped NVDA 25 121"):
            with self.subTest(text=text):
                self.assertIsNotNone(self.parse(text))

    def test_sell_side(self):
        self.assertEqual(self.parse("SOLD MSFT 10 @ 400").side, "sell")

    def test_thousands_separator(self):
        self.assertEqual(self.parse("SOLD MSFT 1,000 @ $1,412.30").quantity, 1000)

    def test_rejects_ambiguous_input(self):
        for text in ("sold everything", "TOOK AAPL", "", "TOOK AAPL 0 @ 10",
                     "TOOK AAPL 10 @ 0", "just checking in", "TOOK AAPL abc @ 10"):
            with self.subTest(text=text):
                self.assertIsNone(self.parse(text))


class TestStageBEndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.TemporaryDirectory()
        for key in ("SCREENER_DATA", "SCREENER_JOURNAL", "SCREENER_LOGS"):
            os.environ[key] = cls.dir.name
        for module in [m for m in sys.modules if m.startswith(("pipeline", "mcp"))]:
            del sys.modules[module]
        from pipeline.stage_a_nightly import run as run_a
        from pipeline.stage_b_brief import run as run_b
        run_a(offline=True, max_symbols=25)
        cls.record = run_b(offline=True, dry_run=True)

    @classmethod
    def tearDownClass(cls):
        cls.dir.cleanup()
        for key in ("SCREENER_DATA", "SCREENER_JOURNAL", "SCREENER_LOGS"):
            os.environ.pop(key, None)

    def test_produced_a_record(self):
        for key in ("as_of", "regime", "picks", "message", "delivered"):
            self.assertIn(key, self.record)

    def test_regime_computed_not_degraded(self):
        """Offline must exercise the real regime path, benchmark included --
        otherwise that code is only ever run in production."""
        self.assertIn(self.record["regime"], ("RISK_ON", "NEUTRAL", "RISK_OFF"))
        self.assertNotIn("unavailable", " ".join(self.record["regime_reasons"]))

    def test_message_is_telegram_sized(self):
        self.assertLess(len(self.record["message"]), 4096)
        self.assertGreater(len(self.record["message"]), 40)

    def test_picks_carry_a_full_plan(self):
        for pick in self.record["picks"]:
            for key in ("entry", "stop", "target", "shares"):
                self.assertIn(key, pick["plan"])
            self.assertTrue(pick["thesis"])
            self.assertTrue(pick["invalidation"])

    def test_picks_within_cap(self):
        from pipeline.stage_b_brief import MAX_PICKS
        self.assertLessEqual(len(self.record["picks"]), MAX_PICKS)

    def test_journal_written(self):
        from mcp.journal import Journal
        self.assertEqual(len(Journal(self.dir.name).read_briefs()), 1)

    def test_heartbeat_written(self):
        beat = json.loads((Path(self.dir.name) / "heartbeat_b.json").read_text())
        self.assertEqual(beat["stage"], "B")

    def test_stale_candidates_refused(self):
        from pipeline import paths
        from pipeline.stage_b_brief import StageBError, load_candidates
        paths.CANDIDATES.write_text(json.dumps({"run": {"as_of": "2020-01-01"},
                                                "candidates": []}))
        with self.assertRaisesRegex(StageBError, "days old"):
            load_candidates()

    def test_missing_candidates_refused(self):
        from pipeline.stage_b_brief import StageBError, load_candidates
        with self.assertRaisesRegex(StageBError, "did not run"):
            load_candidates(Path(self.dir.name) / "nope.json")


if __name__ == "__main__":
    unittest.main(verbosity=1)
