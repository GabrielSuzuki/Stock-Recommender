#!/usr/bin/env python3
"""One-command Telegram setup: token in, ready-to-paste .env out.

    python3 notify/setup_bot.py 8123456789:AAF-xxxxxxxxxxxxxxxxxxxxxxxxx

Finding your chat id is the fiddly step -- it involves messaging the bot, then
calling getUpdates and reading a nested JSON field. This does that for you:
verifies the token, waits for you to send the bot a message, extracts the chat
id, sends a confirmation, and prints the exact lines to paste into `.env`.

If you already messaged the bot before running this, it picks that up too.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

API = "https://api.telegram.org"


def verify_token(token: str) -> dict | None:
    """getMe is the cheapest way to tell a bad token from a bad chat id."""
    try:
        resp = requests.get(f"{API}/bot{token}/getMe", timeout=(5, 15))
    except requests.RequestException as exc:
        print(f"  network error: {exc}")
        return None

    if resp.status_code == 401:
        print("  401 Unauthorized — the token is wrong or has been revoked.")
        print("  In BotFather: /mybots -> your bot -> API Token")
        return None
    if resp.status_code == 404:
        print("  404 — the token is malformed. It must include the numeric part")
        print("  before the colon, e.g. 8123456789:AAF-xxxxxxxx")
        return None
    if resp.status_code != 200:
        print(f"  unexpected {resp.status_code}: {resp.text[:200]}")
        return None
    return resp.json().get("result", {})


def find_chat(token: str, wait_seconds: int = 120) -> dict | None:
    """Poll getUpdates until the user messages the bot."""
    deadline = time.monotonic() + wait_seconds
    printed_hint = False

    while time.monotonic() < deadline:
        try:
            resp = requests.get(f"{API}/bot{token}/getUpdates",
                                params={"timeout": 10}, timeout=(5, 20))
            resp.raise_for_status()
            updates = resp.json().get("result", [])
        except requests.RequestException as exc:
            print(f"  poll failed ({exc}); retrying")
            time.sleep(3)
            continue

        for update in reversed(updates):
            message = update.get("message") or update.get("edited_message") or {}
            chat = message.get("chat")
            if chat and chat.get("id"):
                return chat

        if not printed_hint:
            print("  waiting for you to message the bot...")
            printed_hint = True
        time.sleep(2)

    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Set up the Telegram bot")
    parser.add_argument("token", help="the token BotFather gave you")
    parser.add_argument("--alert-token", default=None,
                        help="optional second bot, for failure alerts only")
    parser.add_argument("--wait", type=int, default=120)
    args = parser.parse_args(argv)

    token = args.token.strip()
    print("\n1. Verifying the token")
    bot = verify_token(token)
    if bot is None:
        return 1
    print(f"  ok — @{bot.get('username')} ({bot.get('first_name')})")

    print(f"\n2. Open Telegram, find @{bot.get('username')}, press Start "
          "(or send it any message)")
    chat = find_chat(token, args.wait)
    if chat is None:
        print("\n  Timed out. The bot cannot message you until you message it")
        print("  first — Telegram blocks unsolicited bot messages. Press Start")
        print("  in the chat and run this again.")
        return 1

    chat_id = chat["id"]
    who = chat.get("username") or chat.get("first_name") or "you"
    kind = chat.get("type", "private")
    print(f"  found chat {chat_id} ({kind}, {who})")

    print("\n3. Sending a confirmation")
    try:
        sent = requests.post(f"{API}/bot{token}/sendMessage",
                             data={"chat_id": chat_id,
                                   "text": "Setup complete. Your morning brief "
                                           "will arrive here at 5am Pacific."},
                             timeout=(5, 20))
        print("  delivered — check your phone" if sent.status_code == 200
              else f"  send failed: {sent.text[:200]}")
    except requests.RequestException as exc:
        print(f"  send failed: {exc}")

    alert_id = chat_id
    if args.alert_token:
        print("\n4. Second bot for alerts")
        alert_bot = verify_token(args.alert_token.strip())
        if alert_bot:
            print(f"  ok — @{alert_bot.get('username')}; now message it too")
            alert_chat = find_chat(args.alert_token.strip(), args.wait)
            if alert_chat:
                alert_id = alert_chat["id"]
                print(f"  found alert chat {alert_id}")

    print("\n" + "=" * 62)
    print("Paste these into .env:")
    print("=" * 62)
    print(f"TELEGRAM_BOT_TOKEN={token}")
    print(f"TELEGRAM_CHAT_ID={chat_id}")
    print(f"TELEGRAM_ALERT_CHAT_ID={alert_id}")
    if args.alert_token:
        print(f"# alert bot token: {args.alert_token.strip()}")
    print("=" * 62)
    print("\nThen: chmod 600 .env && python3 notify/send_test.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
