#!/usr/bin/env python3
"""Reply-to-log — turn Telegram replies into journal entries.

    python3 -m pipeline.fill_listener            # one poll, then exit
    python3 -m pipeline.fill_listener --watch    # long-poll continuously

Run it on a timer during market hours. Replying

    TOOK AAPL 100 @ 182.50

to the morning brief writes a fill to the journal and gets a ✓ back. Without
fills there is nothing for the weekly review to join against, so this small
piece is what makes the whole feedback loop work.

Two things it must get right:

  - **Never process an update twice.** Telegram's getUpdates is
    at-least-once: an update stays queued until you acknowledge it with a
    higher offset. The offset is persisted to disk, so a crash between "wrote
    the fill" and "acknowledged" would otherwise double-log the trade. We
    persist BEFORE acknowledging, so the worst case is a duplicate-detection
    skip rather than a lost or doubled fill.

  - **Never guess.** A message that does not parse gets a short "didn't
    understand" reply and is dropped. A mis-parsed fill silently corrupts
    every future review.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp.journal import Journal, parse_fill_reply   # noqa: E402
from pipeline import paths                          # noqa: E402

log = logging.getLogger("fill_listener")

HELP_TEXT = (
    "Didn't understand that. Use:\n"
    "TOOK AAPL 100 @ 182.50\n"
    "(also: BOUGHT / SOLD / STOPPED / CLOSED)"
)


class OffsetStore:
    """Persisted getUpdates offset, so restarts don't replay the queue."""

    def __init__(self, path: Path):
        self.path = path

    def read(self) -> int | None:
        if not self.path.exists():
            return None
        try:
            return int(json.loads(self.path.read_text())["offset"])
        except (ValueError, KeyError, TypeError):
            log.warning("offset file unreadable; starting from the queue head")
            return None

    def write(self, offset: int) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"offset": offset,
                                   "updated": datetime.now().astimezone().isoformat()}))
        tmp.replace(self.path)


def _seen_key(update: dict) -> str:
    return str(update.get("update_id", ""))


def process_updates(updates: list[dict], journal: Journal, notifier) -> dict:
    """Parse each update, journal the fills, reply. Returns counts."""
    counts = {"seen": len(updates), "fills": 0, "unparsed": 0, "ignored": 0}
    logged_ids = {str(f.get("update_id")) for f in journal.read_fills()}

    for update in updates:
        message = update.get("message") or update.get("edited_message") or {}
        text = (message.get("text") or "").strip()
        if not text:
            counts["ignored"] += 1
            continue

        update_id = _seen_key(update)
        if update_id and update_id in logged_ids:
            # Telegram is at-least-once. Already-journalled updates are
            # skipped rather than double-logged.
            counts["ignored"] += 1
            continue

        fill = parse_fill_reply(text)
        if fill is None:
            counts["unparsed"] += 1
            log.info("unparsed reply: %r", text[:80])
            notifier.send(HELP_TEXT, parse_mode="")
            continue

        journal.record_fill({
            **fill.to_dict(),
            "update_id": update_id,
            "raw": text,
            "message_date": _iso(message.get("date")),
        })
        counts["fills"] += 1
        log.info("logged fill: %s %s %s @ %s",
                 fill.action, fill.quantity, fill.symbol, fill.price)
        notifier.send(
            f"✓ {fill.action} {fill.symbol} {fill.quantity} @ {fill.price:.2f}",
            parse_mode="")

    return counts


def poll_once(timeout: int = 0) -> dict:
    paths.ensure_dirs()
    paths.load_dotenv()

    from notify.telegram import Telegram

    telegram = Telegram()
    journal = Journal(paths.JOURNAL_DIR)
    store = OffsetStore(paths.DATA_DIR / "telegram_offset.json")

    offset = store.read()
    updates = telegram.get_updates(offset=offset)
    if not updates:
        return {"seen": 0, "fills": 0, "unparsed": 0, "ignored": 0}

    counts = process_updates(updates, journal, telegram)

    # Persist the offset only after the journal write has landed. A crash
    # between the two replays the update, which duplicate detection catches;
    # the reverse order would lose the fill entirely.
    highest = max(int(u["update_id"]) for u in updates if "update_id" in u)
    store.write(highest + 1)
    telegram.get_updates(offset=highest + 1)      # acknowledge to Telegram
    return counts


def watch(interval: int = 30) -> int:
    log.info("watching for fill replies every %ss (ctrl-c to stop)", interval)
    while True:
        try:
            counts = poll_once()
            if counts["fills"] or counts["unparsed"]:
                log.info("poll: %s", counts)
        except KeyboardInterrupt:
            return 0
        except Exception as exc:                        # noqa: BLE001
            log.error("poll failed: %s", exc)
        time.sleep(interval)


def _iso(epoch) -> str:
    from datetime import timezone
    try:
        return datetime.fromtimestamp(int(epoch), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Log fills from Telegram replies")
    parser.add_argument("--watch", action="store_true", help="poll continuously")
    parser.add_argument("--interval", type=int, default=30)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S")

    if args.watch:
        return watch(args.interval)
    try:
        print(json.dumps(poll_once(), indent=2))
        return 0
    except Exception as exc:                            # noqa: BLE001
        log.error("poll failed: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
