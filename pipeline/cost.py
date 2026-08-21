#!/usr/bin/env python3
"""What has the model actually cost?

    python -m pipeline.cost              # since the beginning
    python -m pipeline.cost --since 1d   # today
    python -m pipeline.cost --dashboard  # self-contained HTML

Reads the tokenwise ledger, which records every call twice: what you paid, and
what the same request would have cost unoptimized. The saving is an audit, not
a marketing figure -- `sum(by_strategy)` equals `baseline - actual` exactly.

Worth checking after the first live Stage B, and then whenever a brief looks
unusual. A spend spike is the earliest signal that something is looping.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "vendor"))

from pipeline import paths  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Claude API spend")
    parser.add_argument("--since", default=None,
                        help="e.g. 1d, 7d, 30d (default: everything)")
    parser.add_argument("--dashboard", action="store_true",
                        help="write a self-contained HTML report")
    args = parser.parse_args(argv)

    paths.load_dotenv()
    from tokenwise import Ledger
    from tokenwise import report as text_report

    # Resolve exactly as pipeline.llm does, environment variable included.
    # A reader and a writer that disagree about the path is a bug that looks
    # like "the feature does not work" rather than "the file is elsewhere".
    from pipeline.llm import ledger_path as resolve_ledger
    ledger_path = Path(resolve_ledger())
    ledger = Ledger(path=str(ledger_path))
    records = ledger.read()

    if not records:
        print(f"No model calls recorded yet.\n  ledger: {ledger_path}")
        print("\nRun `python -m pipeline.stage_b_brief --dry-run` first — Stage A")
        print("never calls the model, so it writes nothing here by design.")
        return 0

    summary = ledger.summary(since=args.since)
    print(text_report.render_text(summary))

    total = summary.get("actual_cost", 0.0)
    days = max(len(summary.get("by_day", {}) or {}), 1)
    print(f"\n  average per day    ${total / days:.3f}")
    print(f"  projected monthly  ${total / days * 21:.2f}  (21 trading days)")

    if args.dashboard:
        from tokenwise import dashboard
        out = paths.LOG_DIR / "tokenwise.html"
        out.parent.mkdir(parents=True, exist_ok=True)
        dashboard.write(summary, str(out))
        print(f"\n  wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
