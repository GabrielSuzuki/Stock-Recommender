"""Fill listener tests. No network."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp.journal import Journal                              # noqa: E402
from pipeline.fill_listener import OffsetStore, process_updates  # noqa: E402


class FakeNotifier:
    def __init__(self):
        self.sent = []

    def send(self, text, **_):
        self.sent.append(text)
        return True


def update(uid: int, text: str) -> dict:
    return {"update_id": uid,
            "message": {"text": text, "date": 1755000000, "chat": {"id": 1}}}


class TestProcessUpdates(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.journal = Journal(self.dir.name)
        self.notifier = FakeNotifier()

    def tearDown(self):
        self.dir.cleanup()

    def test_valid_fill_is_journalled_and_acknowledged(self):
        counts = process_updates([update(1, "TOOK AAPL 100 @ 182.50")],
                                 self.journal, self.notifier)
        self.assertEqual(counts["fills"], 1)
        fills = self.journal.read_fills()
        self.assertEqual(fills[0]["symbol"], "AAPL")
        self.assertEqual(fills[0]["quantity"], 100)
        self.assertIn("✓", self.notifier.sent[0])

    def test_raw_text_is_kept(self):
        process_updates([update(1, "took nvda 25 at 121")], self.journal, self.notifier)
        self.assertEqual(self.journal.read_fills()[0]["raw"], "took nvda 25 at 121")

    def test_unparsed_gets_help_not_a_guess(self):
        counts = process_updates([update(1, "sold some apple")],
                                 self.journal, self.notifier)
        self.assertEqual((counts["fills"], counts["unparsed"]), (0, 1))
        self.assertEqual(self.journal.read_fills(), [])
        self.assertIn("Didn't understand", self.notifier.sent[0])

    def test_empty_message_ignored_silently(self):
        counts = process_updates([{"update_id": 1, "message": {}}],
                                 self.journal, self.notifier)
        self.assertEqual(counts["ignored"], 1)
        self.assertEqual(self.notifier.sent, [])

    def test_duplicate_update_id_not_double_logged(self):
        """getUpdates is at-least-once. A crash between journalling and
        acknowledging replays the update, and it must not double-count."""
        process_updates([update(7, "TOOK AAPL 100 @ 182.50")], self.journal, self.notifier)
        process_updates([update(7, "TOOK AAPL 100 @ 182.50")], self.journal, self.notifier)
        self.assertEqual(len(self.journal.read_fills()), 1)

    def test_different_ids_same_text_both_logged(self):
        """Buying the same name twice in a day is legitimate -- dedupe is on
        update_id, never on the message text."""
        process_updates([update(1, "TOOK AAPL 50 @ 182.50"),
                         update(2, "TOOK AAPL 50 @ 182.50")], self.journal, self.notifier)
        self.assertEqual(len(self.journal.read_fills()), 2)

    def test_edited_message_handled(self):
        counts = process_updates(
            [{"update_id": 3, "edited_message": {"text": "SOLD MSFT 10 @ 400",
                                                 "date": 1755000000}}],
            self.journal, self.notifier)
        self.assertEqual(counts["fills"], 1)

    def test_mixed_batch(self):
        counts = process_updates([
            update(1, "TOOK AAPL 100 @ 182.50"),
            update(2, "good morning"),
            update(3, "SOLD NVDA 25 @ 121"),
        ], self.journal, self.notifier)
        self.assertEqual((counts["fills"], counts["unparsed"]), (2, 1))


class TestOffsetStore(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.store = OffsetStore(Path(self.dir.name) / "offset.json")

    def tearDown(self):
        self.dir.cleanup()

    def test_absent_is_none(self):
        self.assertIsNone(self.store.read())

    def test_roundtrip(self):
        self.store.write(42)
        self.assertEqual(self.store.read(), 42)

    def test_corrupt_file_falls_back_to_none(self):
        self.store.path.write_text("{not json")
        self.assertIsNone(self.store.read())

    def test_write_is_atomic(self):
        self.store.write(1)
        self.store.write(2)
        self.assertEqual(self.store.read(), 2)
        self.assertFalse(self.store.path.with_suffix(".tmp").exists())

    def test_records_a_timestamp(self):
        self.store.write(5)
        self.assertIn("updated", json.loads(self.store.path.read_text()))


if __name__ == "__main__":
    unittest.main(verbosity=1)
