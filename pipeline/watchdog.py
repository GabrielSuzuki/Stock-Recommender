#!/usr/bin/env python3
"""Watchdog — 05:20 Pacific. Did a brief actually go out?

    python3 -m pipeline.watchdog

The failure mode this exists for: the job dies in a way that produces no
exception and no alert -- the box was asleep, the timer was disabled by a
half-finished deploy, the network was down at 05:00 and every retry burned out.
You wake up, see no message, and conclude there were no setups.

Silence must never be ambiguous. If nothing was delivered by 05:20, this says so.
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import paths  # noqa: E402

MAX_BRIEF_AGE_MINUTES = 45
MAX_CANDIDATES_AGE_HOURS = 12


def check() -> list[str]:
    """Return a list of problems. Empty means all good."""
    problems: list[str] = []
    now = datetime.now().astimezone()

    beat_file = paths.DATA_DIR / "heartbeat_b.json"
    if not beat_file.exists():
        problems.append("no Stage B heartbeat file — the brief has never run")
    else:
        try:
            beat = json.loads(beat_file.read_text())
            when = datetime.fromisoformat(beat["completed_at"])
            age = (now - when).total_seconds() / 60
            if age > MAX_BRIEF_AGE_MINUTES:
                problems.append(
                    f"last brief was {age:.0f} min ago ({when:%Y-%m-%d %H:%M}), "
                    f"expected within {MAX_BRIEF_AGE_MINUTES} min"
                )
        except Exception as exc:                       # noqa: BLE001
            problems.append(f"Stage B heartbeat unreadable: {exc}")

    if not paths.CANDIDATES.exists():
        problems.append("candidates.json missing — Stage A did not produce output")
    else:
        age_h = (now.timestamp() - paths.CANDIDATES.stat().st_mtime) / 3600
        if age_h > MAX_CANDIDATES_AGE_HOURS:
            problems.append(f"candidates.json is {age_h:.1f}h old — Stage A is not running")
        else:
            try:
                payload = json.loads(paths.CANDIDATES.read_text())
                as_of = date.fromisoformat(payload["run"]["as_of"])
                if (date.today() - as_of) > timedelta(days=4):
                    problems.append(f"candidates.json is for {as_of} — stale prices")
            except Exception as exc:                   # noqa: BLE001
                problems.append(f"candidates.json unreadable: {exc}")

    return problems


def main() -> int:
    paths.load_dotenv()
    problems = check()
    if not problems:
        print("watchdog: ok")
        return 0

    message = "watchdog: no brief delivered this morning\n\n" + "\n".join(
        f"- {p}" for p in problems)
    print(message, file=sys.stderr)
    try:
        from notify.telegram import Telegram
        Telegram().alert(message)
    except Exception as exc:                           # noqa: BLE001
        print(f"could not send alert: {exc}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
