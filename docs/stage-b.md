# Stage B — the morning brief

Runs at **05:00 Pacific, Mon–Fri**. Reads `candidates.json`, decides, pushes to Telegram. Market opens 06:30, so you have ~80 minutes to read, disagree, and set orders.

```bash
python3 -m pipeline.stage_b_brief --offline --dry-run   # no keys, nothing sent
python3 -m pipeline.stage_b_brief --dry-run             # live data, prints instead of sending
python3 -m pipeline.stage_b_brief                       # the real thing
```

---

## Order of operations

| | Step | Cost |
|---|---|---|
| 1 | **regime** — computed in Python. RISK_OFF stops everything. | free |
| 2 | **news** — only for the ~20 screened names, never the universe | free tier |
| 3 | **disqualify** — mechanical rules drop names *before* the model sees them | free |
| 4 | **catalysts** — one Haiku call per survivor, in parallel | ~$0.13 |
| 5 | **theses** — one Opus call over all survivors together | ~$0.11 |
| 6 | **render** — deterministic. The model writes one sentence. | ~$0.01 |
| 7 | **deliver** — Telegram → journal → heartbeat | free |

A message goes out on **every** path, including failure. Silence must never be ambiguous: if no message arrives, something is broken, and that is the only thing silence is allowed to mean.

---

## Two deliberate departures from the architecture doc

Both move work *out* of the model. Both were written up in the design as agent responsibilities, and both are better as code.

### The regime verdict is computed, not generated

The original design had a `market-regime` agent produce the RISK_ON / RISK_OFF label. That is the wrong shape for a component whose entire job is to say no.

A model asked "is the tape healthy?" every morning will occasionally say yes on a day the rules say no — and it will say it persuasively. The one thing you cannot afford to have argued out of is the off-switch. So `core/regime.py` computes the verdict from thresholds in `config/screen.yaml`, and the model is left with the job it is actually better at: writing the sentence that explains the verdict to you at 5 AM.

`RegimeConfig.validate()` also refuses a config where the NEUTRAL band is stricter than the RISK_OFF band — invert one threshold by accident and RISK_OFF becomes unreachable, which is a broken off-switch that looks like a working one. There's a test for each direction.

**A degraded reading can never be RISK_ON.** If the benchmark is missing or breadth can't be computed, the verdict falls to NEUTRAL. The cost of a wrongly cautious day is a missed trade; the cost of a wrongly confident one is a drawdown.

### The brief is rendered, not composed

The original design had `brief-editor` "compress everything into a Telegram-shaped message." That hands a model three jobs it is bad at and that fail silently:

- **valid MarkdownV2** — one unescaped `.` and Telegram rejects the entire message
- **staying under 4096 characters**
- **not altering a number on the way through**

The first two are formatting bugs that only appear at 05:00. The third is a correctness bug you might never notice. And `thesis-writer` already caps each thesis at two sentences, so there was very little compression left to do — what remained was risk.

So: layout is code, escaping goes through `escape_md`, length is enforced by construction (over-long input drops the weakest pick rather than splitting mid-thesis), and every number is interpolated straight from `candidates.json`. The model writes the one-line market summary and nothing else.

---

## Mechanical disqualification

`mcp/news/disqualify.py`. The architecture doc says these are "non-negotiable and mechanical, not judgment calls," so they're deterministic Python — not a prompt instruction that might land differently on a given morning.

| Rule | Source |
|---|---|
| earnings inside the 21-day holding window | Finnhub earnings calendar |
| registration / takedown (S-1, S-3, 424B) | EDGAR form type |
| going-private (SC 13E3) | EDGAR form type |
| reverse split | headline regex |
| going-concern language | headline regex |
| acquisition at a fixed price | headline regex |
| offering / ATM announced | headline regex |
| bankruptcy, delisting notice | headline regex |

A disqualified name never reaches the model. That's roughly 20–30% of a typical day's candidates and the cheapest token saving in the pipeline — there's nothing to reason about once the answer is already no.

The model may add its own concerns and can disqualify further. It **cannot clear** a disqualification the rules have set.

One rule worth noting: a 4-for-1 *forward* split is bullish housekeeping, only the *reverse* kind is disqualifying. Getting that backwards would silently drop good names, so there's a test.

---

## Degraded modes

Nothing here is allowed to take down the morning.

| Failure | Behaviour |
|---|---|
| Regime can't be computed | NEUTRAL, `degraded: true`, flagged in the brief |
| News provider down for a symbol | That name briefs with "no news available" |
| EDGAR unavailable | Filing-based rules skipped, run continues |
| `catalyst-analyst` fails on one name | That name keeps its technical setup, catalyst read marked missing |
| `thesis-writer` fails | Falls back to top 3 by rank with placeholder theses, marked `degraded` |
| Market-line agent fails | Falls back to the regime rule text verbatim |
| Telegram send fails | Logged; the journal still records the brief |
| `candidates.json` missing or >4 days old | **Refuses to run**, sends an alert |

`DEGRADED RUN` appears in the brief footer whenever any of this fired, so a thin brief never passes for a confident one.

---

## The journal

`mcp/journal/` — append-only JSONL, two record types.

**`briefs.jsonl`** gets a line every morning, including RISK_OFF days and days nothing passed. The empty days are not filler: a review that only sees the days you traded can't tell you what the regime filter *saved* you, which is the most valuable thing the journal can measure.

**`fills.jsonl`** is populated by replying to the Telegram message:

```
TOOK AAPL 100 @ 182.50
```

The parser is deliberately strict — a reply that doesn't match is left alone rather than guessed at. A mis-parsed fill silently corrupts every future review, and asking you to retype one message costs nothing by comparison. `bought`, `sold`, `stopped`, `closed`, `at` instead of `@`, `$`, and thousands separators all work.

---

## The VIX problem

There is no VIX on the free data stack — Alpaca doesn't carry index products on the IEX feed. The regime uses **SPY's own 20-day annualized realized volatility** instead.

Two honest caveats: it's backward-looking where VIX is forward-looking, so it registers a shock a day or two late; and it has no volatility-risk-premium component, so its *levels are not comparable to VIX levels*. The thresholds in `config/screen.yaml` are calibrated for this measure — do not copy VIX rules of thumb into them.

---

## The benchmark is not an index constituent

`SPY` is an ETF, so it is not in the S&P 500 constituent list and would never have been fetched. Stage B would then compute the regime with no benchmark, degrade to NEUTRAL *every single day*, and the RISK_OFF branch would be unreachable — a broken off-switch that looks like a working one, which is exactly the failure `RegimeConfig.validate()` guards against from the other direction.

Stage A now appends the benchmark to the fetch list explicitly and drops it before screening (it's an ETF; it has no business in a Trend Template screen). The offline path includes a synthetic SPY too, so the regime code is genuinely exercised on every offline run rather than only in production.
