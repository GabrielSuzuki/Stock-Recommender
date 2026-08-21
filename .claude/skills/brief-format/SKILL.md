---
name: brief-format
description: The Telegram brief layout, MarkdownV2 escaping rules, and why the brief is rendered rather than model-composed. Use when changing pipeline/agents/editor.py, notify/telegram.py, or the shape of the morning message.
---

# Brief format

## The brief is rendered, not composed

The model writes **one sentence** — the market line. Everything else is code.

Letting a model emit MarkdownV2 hands it three jobs it fails silently at:

1. **Valid escaping.** One unescaped `.` and Telegram rejects the whole message.
2. **Length.** 4096-character cap, enforced here by construction.
3. **Not altering numbers in transit.** The correctness bug you might never
   notice.

`thesis-writer` already caps each thesis at two sentences, so there was little
compression left to do — what remained was risk.

## Escaping

MarkdownV2 reserves ``_ * [ ] ( ) ~ ` > # + - = | { } . !``

**Wrap every dynamic value in `escape_md()`.** Prices, tickers, headlines,
dates. Do *not* escape the markup you are deliberately writing, or your bold
markers become literal asterisks.

`Telegram.send()` retries as plain text if MarkdownV2 fails, so content still
arrives — but fix the escaping rather than relying on the fallback.

## Layout

```
*Morning Brief — Thu 20 Aug*
🟢 *RISK_ON* — <one model-written sentence>

*AAPL*  ★★★★
entry 182.50  ·  stop 176.20  ·  target 198.25
29 sh  ·  risk $183
<thesis, ≤2 sentences>
_invalid if_ <specific condition>
_earnings 2026-10-04 (45d out)_        ← only if outside the hold window

heat 3.2% · 4 disqualified · $0.24 · saved 78%
_Reply_ `TOOK TICKER QTY @ PRICE` _to log a fill._
```

Over-long input **drops the weakest pick** rather than splitting mid-thesis.

## Non-negotiables

- **Send on every path.** RISK_OFF days and empty-screen days both push a
  message. Silence must only ever mean *broken*.
- **`DEGRADED RUN` in the footer** whenever any component fell back, so a thin
  brief never passes for a confident one.
- **The reply hint is always present** — it is what makes the journal, and
  therefore the weekly review, possible at all.
- **One message.** If it needs scrolling you will stop reading it by week three.
