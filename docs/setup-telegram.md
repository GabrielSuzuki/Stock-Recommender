# Telegram setup

About 10 minutes. At the end you'll have a bot that pushes your morning brief to your phone, a separate alert path for failures, and a verified round trip.

---

## 1. Create the bot

1. Open Telegram, search for **@BotFather** (blue check — there are impostors).
2. Send `/newbot`.
3. Display name: `Swing Brief` (anything you like).
4. Username: must end in `bot` and be globally unique — e.g. `gab_swing_brief_bot`.
5. BotFather replies with a token like `8123456789:AAF-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx`.

**That token is a password.** Anyone holding it can post as your bot and read anything sent to it. Never commit it. If it leaks, `/revoke` in BotFather.

While you're there, worth setting:

```
/setdescription   Morning swing-trade brief
/setuserpic       (optional)
/setprivacy       → Disable
```

`/setprivacy → Disable` matters if you want the reply-to-log feature later, since the bot needs to read your replies.

---

## 2. Get your chat ID

**The quick way:**

```bash
python3 notify/setup_bot.py <YOUR_TOKEN>
```

It verifies the token, waits for you to message the bot, finds the chat ID,
sends a confirmation, and prints the exact `.env` lines to paste. Skip to step 4
if that worked.

**The manual way**, if you'd rather see what's happening:

The bot cannot message you until you message it first — Telegram blocks unsolicited bot messages.

1. Search your bot's username, open the chat, press **Start** (or send `hi`).
2. Then in a terminal:

```bash
curl -s "https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates" | python3 -m json.tool
```

Look for `"chat": {"id": 987654321, ...}`. That number is your `TELEGRAM_CHAT_ID`. A personal chat ID is positive; group IDs are negative and usually start `-100`.

If `result` is an empty list, you haven't actually sent the bot a message yet.

---

## 3. Optional: a second bot for alerts

Recommended. Failure alerts going to the same chat as the brief means a crash notice sits below three days of green checkmarks and you miss it. Repeat step 1 with a second bot named `Swing Alerts`, mute the brief chat, and leave alerts unmuted. Its chat ID goes in `TELEGRAM_ALERT_CHAT_ID`.

Skip this if you'd rather not manage two bots — the code falls back to the main chat.

---

## 4. Store the credentials

Create `.env` in the repo root (already in `.gitignore`):

```bash
cp .env.example .env
chmod 600 .env          # readable only by you
```

Fill in:

```
TELEGRAM_BOT_TOKEN=8123456789:AAF-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TELEGRAM_CHAT_ID=987654321
TELEGRAM_ALERT_CHAT_ID=987654321
```

---

## 5. Verify

```bash
pip install requests
python3 notify/send_test.py
```

You should get three messages: a plain connectivity line, a formatted two-name brief, and a ⚠️ alert.

```
1/3  plain message ... ok
2/3  formatted brief ... ok
3/3  alert path ... ok
```

**If 1 passes but 2 fails**, it's a MarkdownV2 escaping bug — some dynamic value reached the message without going through `escape_md()`. The notifier auto-retries as plain text so you'll still receive it, but fix the escaping.

**If 1 fails with 401**, the token is wrong. **404** means the token is malformed (missing the part before the colon). **400 chat not found** means the chat ID is wrong or you never pressed Start.

---

## 6. What the code gives you

`notify/telegram.py`:

| Call | Use |
|---|---|
| `Telegram().send(text)` | Brief delivery. Auto-splits over 4096 chars, falls back to plain text if MarkdownV2 fails. |
| `Telegram().send_photo(path, caption)` | One chart per name. Caption truncated to Telegram's 1024 limit. |
| `Telegram().alert(msg)` | Failure path. No parse mode, so a stack trace with underscores can't itself fail to send. |
| `Telegram().heartbeat(stage)` | "Stage A completed" — proof the job ran. |
| `escape_md(value)` | **Wrap every dynamic value with this.** Prices, tickers, headlines. |
| `build_brief(...)` | Renders the standard brief, including the RISK_OFF stand-down message. |

Three design decisions worth knowing:

- **It never raises.** A broken notifier returns `False` and logs. It must not be able to kill a pipeline that otherwise succeeded.
- **It sends on empty days.** `RISK_OFF` and "nothing passed the screen" both produce a message. Silence has to mean *broken*, never *nothing to say*.
- **It paces at 1.05s between sends** and honours the `retry_after` field on HTTP 429, because Telegram's per-chat limit is roughly one message per second.

---

## 7. Later: reply-to-log

Once the brief is running, `Telegram().get_updates()` polls your replies. Reply to a brief with:

```
TOOK AAPL 100 @ 182.50
```

and a small parser writes the fill to the journal. This is what makes the weekly review possible — without recorded fills there's nothing to review. Worth adding in week 3, not before.
