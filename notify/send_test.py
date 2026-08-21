#!/usr/bin/env python3
"""Verify Telegram delivery end to end. Run this before wiring anything else.

    python3 notify/send_test.py

Reads TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID from the environment or ../.env
"""
import os
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from notify.telegram import Telegram, build_brief  # noqa: E402

SAMPLE = [
    dict(ticker="AAPL", thesis="Base breakout on 3x average volume; sector RS in top 3.",
         entry="182.50", stop="176.20", target="198.00", rr="2.5", shares="54",
         invalidation="daily close below 176.20"),
    dict(ticker="NVDA", thesis="Held 50-day on the pullback; 52-wk high within 4%.",
         entry="121.80", stop="115.00", target="140.00", rr="2.7", shares="73",
         invalidation="loses the 50-day MA on volume"),
]


def main() -> int:
    try:
        tg = Telegram()
    except RuntimeError as exc:
        print(f"config error: {exc}")
        return 2

    print("1/3  plain message ...", end=" ", flush=True)
    print("ok" if tg.send("tokenwise pipeline: connectivity test", parse_mode="") else "FAILED")

    print("2/3  formatted brief ...", end=" ", flush=True)
    brief = build_brief("RISK_ON", "SPY above 50MA, breadth 62%", SAMPLE, str(date.today()))
    ok_brief = tg.send(brief)
    print("ok" if ok_brief else "FAILED")

    print("3/3  alert path ...", end=" ", flush=True)
    print("ok" if tg.alert("this is what a failure looks like") else "FAILED")

    if not ok_brief:
        print("\nThe formatted brief failed but plain text worked -> MarkdownV2 escaping bug.")
        return 1
    print("\nAll three delivered. Check your phone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
