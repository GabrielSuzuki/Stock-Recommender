"""Offline tests for the Telegram notifier. No token or network required."""
import sys, types, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from notify.telegram import (escape_md, chunk_text, build_brief, Telegram,
                             TelegramConfig, TEXT_LIMIT, _MDV2_RESERVED)


class FakeResp:
    def __init__(self, status=200, body=None, text=""):
        self.status_code, self._body, self.text = status, body or {}, text
    def json(self): return self._body


class FakeSession:
    def __init__(self, script):
        self.script, self.calls = list(script), []
    def post(self, url, data=None, files=None, timeout=None):
        # Snapshot the payload: the caller must be free to reuse or rebuild it.
        self.calls.append({"url": url, "data": dict(data or {}), "files": files})
        return self.script.pop(0) if self.script else FakeResp(200)


CFG = TelegramConfig(bot_token="T", chat_id="123", alert_chat_id="456")


class TestEscape(unittest.TestCase):
    def test_every_reserved_char_is_escaped(self):
        for ch in _MDV2_RESERVED:
            self.assertEqual(escape_md(ch), "\\" + ch, f"{ch!r} not escaped")

    def test_plain_text_untouched(self):
        self.assertEqual(escape_md("AAPL 182 usd"), "AAPL 182 usd")

    def test_realistic_headline(self):
        got = escape_md("Q3 beat (+12.4%) — guidance raised!")
        self.assertEqual(got, "Q3 beat \\(\\+12\\.4%\\) — guidance raised\\!")

    def test_non_string_input(self):
        self.assertEqual(escape_md(182.5), "182\\.5")


class TestChunk(unittest.TestCase):
    def test_short_text_single_chunk(self):
        self.assertEqual(chunk_text("hello"), ["hello"])

    def test_empty(self):
        self.assertEqual(chunk_text(""), [])

    def test_all_chunks_within_limit(self):
        text = "\n\n".join(f"para {i} " + "x" * 300 for i in range(60))
        chunks = chunk_text(text)
        self.assertGreater(len(chunks), 1)
        for c in chunks:
            self.assertLessEqual(len(c), TEXT_LIMIT)

    def test_no_content_lost(self):
        text = "\n\n".join(f"p{i}" + "y" * 400 for i in range(40))
        joined = "".join(chunk_text(text)).replace("\n", "")
        self.assertEqual(joined.replace(" ", ""), text.replace("\n", "").replace(" ", ""))

    def test_split_boundaries_never_land_on_odd_backslashes(self):
        # A split chunk ending in a lone backslash would escape the newline we
        # add and corrupt parsing. Only interior boundaries are our
        # responsibility -- if the *source* text ends in a stray backslash it
        # was already invalid MarkdownV2 before it reached us.
        text = escape_md("a.b-c(d)" * 3000)
        chunks = chunk_text(text)
        self.assertGreater(len(chunks), 1)
        for c in chunks[:-1]:
            trailing = len(c) - len(c.rstrip("\\"))
            self.assertEqual(trailing % 2, 0, "chunk ends on odd backslash run")

    def test_hard_cut_when_no_break_points(self):
        chunks = chunk_text("z" * 10000)
        self.assertEqual(len(chunks), 3)
        for c in chunks:
            self.assertLessEqual(len(c), TEXT_LIMIT)


class TestSend(unittest.TestCase):
    def test_success(self):
        s = FakeSession([FakeResp(200)])
        self.assertTrue(Telegram(CFG, s).send("hi"))
        self.assertEqual(len(s.calls), 1)
        self.assertEqual(s.calls[0]["data"]["chat_id"], "123")

    def test_429_is_retried_honoring_retry_after(self):
        s = FakeSession([FakeResp(429, {"parameters": {"retry_after": 0}}), FakeResp(200)])
        self.assertTrue(Telegram(CFG, s).send("hi"))
        self.assertEqual(len(s.calls), 2)

    def test_400_falls_back_to_plain_text(self):
        # First attempt with MarkdownV2 400s; retry without parse_mode succeeds.
        s = FakeSession([FakeResp(400, text="can't parse entities"), FakeResp(200)])
        self.assertTrue(Telegram(CFG, s).send("*bad_markup"))
        self.assertEqual(len(s.calls), 2)
        self.assertIn("parse_mode", s.calls[0]["data"])
        self.assertNotIn("parse_mode", s.calls[1]["data"])

    def test_400_twice_returns_false_not_raises(self):
        s = FakeSession([FakeResp(400, text="bad"), FakeResp(400, text="bad")])
        self.assertFalse(Telegram(CFG, s).send("x"))

    def test_network_error_never_raises(self):
        import requests
        class Boom(FakeSession):
            def post(self, *a, **k):
                self.calls.append(1)
                raise requests.RequestException("down")
        t = Telegram(CFG, Boom([]))
        t.session.script = []
        import notify.telegram as tg
        tg.time.sleep = lambda *_: None          # don't actually back off in tests
        self.assertFalse(t.send("x"))

    def test_alert_uses_alert_chat_and_no_parse_mode(self):
        s = FakeSession([FakeResp(200)])
        Telegram(CFG, s).alert("stage A failed: KeyError('close')")
        self.assertEqual(s.calls[0]["data"]["chat_id"], "456")
        self.assertNotIn("parse_mode", s.calls[0]["data"])

    def test_long_message_sends_multiple_calls(self):
        s = FakeSession([FakeResp(200)] * 5)
        Telegram(CFG, s).send("q" * 9000)
        self.assertEqual(len(s.calls), 3)


class TestBrief(unittest.TestCase):
    PICK = dict(ticker="AAPL", thesis="Base breakout on 3x volume; sector RS top-3.",
                entry="182.50", stop="176.20", target="198.00", rr="2.5",
                shares="54", invalidation="close below 176.20")

    def test_risk_off_has_no_picks(self):
        out = build_brief("RISK_OFF", "SPY below 50MA, VIX +18% w/w",
                          [self.PICK], "2026-08-20")
        self.assertIn("No new entries today", out)
        self.assertNotIn("AAPL", out)

    def test_empty_picks_on_risk_on(self):
        out = build_brief("RISK_ON", "breadth healthy", [], "2026-08-20")
        self.assertIn("no setups passed the screen", out)

    def test_pick_rendered_and_escaped(self):
        out = build_brief("RISK_ON", "breadth healthy", [self.PICK], "2026-08-20")
        self.assertIn("*AAPL*", out)
        self.assertIn("182\\.50", out)      # period escaped
        self.assertIn("2026\\-08\\-20", out) # hyphens escaped
        self.assertIn("TOOK TICKER QTY", out)

    def test_brief_fits_one_message(self):
        picks = [dict(self.PICK, ticker=f"TCK{i}") for i in range(5)]
        out = build_brief("RISK_ON", "breadth healthy", picks, "2026-08-20")
        self.assertLessEqual(len(out), TEXT_LIMIT)

    def test_no_unescaped_reserved_chars_in_dynamic_fields(self):
        nasty = dict(self.PICK, thesis="Beat (+12.4%) — raised guide!")
        out = build_brief("RISK_ON", "ok", [nasty], "2026-08-20")
        self.assertIn("\\(\\+12\\.4%\\)", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
