#!/usr/bin/env python3
"""Send a failure alert. Invoked by systemd's ExecStopPost.

    python3 -m pipeline.notify_failure stage_b

systemd runs ExecStopPost on every exit, success or failure, so this reads
$SERVICE_RESULT and $EXIT_STATUS and stays quiet when the run was clean --
otherwise you would get an alert every single morning and stop reading them
inside a week.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import paths  # noqa: E402


def recent_log(unit: str, lines: int = 12) -> str:
    try:
        out = subprocess.run(
            ["journalctl", "-u", unit, "-n", str(lines), "--no-pager", "-o", "cat"],
            capture_output=True, text=True, timeout=10,
        )
        return out.stdout.strip()[-1200:]
    except Exception:                                  # noqa: BLE001
        return "(journal unavailable)"


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    stage = argv[0] if argv else "unknown"

    result = os.environ.get("SERVICE_RESULT", "")
    status = os.environ.get("EXIT_STATUS", "")
    if result in ("", "success"):
        return 0

    paths.load_dotenv()
    unit = f"screener-{'brief' if stage == 'stage_b' else 'nightly'}.service"
    body = (
        f"{stage} FAILED\n"
        f"result={result} exit={status}\n\n"
        f"{recent_log(unit)}"
    )

    try:
        from notify.telegram import Telegram
        Telegram().alert(body)
    except Exception as exc:                           # noqa: BLE001
        print(f"could not send alert: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
