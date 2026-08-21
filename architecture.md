# Stock Recommender — Research & Architecture

**Version 1.1 · Aug 20, 2026 · Author: Claude (research pass + decisions locked)**

---

## 0. Scope locked for this design

| Decision | Choice |
|---|---|
| Deliverable | Research + architecture (no code yet) |
| Horizon | **Swing trades — days to weeks** |
| Data budget | Free / low-cost APIs ($0–30/mo) |
| Delivery | **Telegram bot** push |
| Schedule | Every morning, **5:00 AM Pacific** |
| Universe | **S&P 500** |
| Screen | **Strict 8-of-8 Trend Template** (trust the empty list) |
| Fundamental overlay | **Deferred to v2** |
| Position awareness | **Telegram reply-to-log** |
| Host | **Hetzner CX22** (~$5.20/mo) |
| Cost layer | **tokenwise** (vendored) |

*Decisions 1–5 from §15 are locked as of Aug 20, 2026. Setup walkthroughs live in `docs/`.*

Everything below is built around those four. If any changes, sections 6, 7 and 9 are the ones that move.

---

## 1. Read this before you build anything

You sent four videos. Two are Humbled Trader building an AI trading workflow with Claude Code + TradingView + Codex; two are TradingLab explainers on day trading and candlestick patterns. That mix tells me the goal is real but the trading methodology isn't nailed down yet — which is the right time to have this conversation.

**Three things that will decide whether this project is worth your time:**

**1. The system's job is not to find alpha. It is to enforce a process.**
An LLM reading news at 5 AM does not have an information edge over the market. What a well-built morning system *does* give you is: the same screen run identically every day, no skipped steps, a written thesis for every name before you're emotionally in it, position sizing computed rather than felt, and a logged record you can audit in six months. That's genuinely valuable — it's just a different kind of valuable than "the AI picks winners."

**2. Candlestick patterns are the weakest link you could build on.**
The largest clean study on this (Tharavanij et al., SET50, *SAGE Open* 2017) found most candlestick reversal patterns produce **no statistically significant mean return**; where returns were significant they ran 0.07%–0.84% against standard deviations of 4.36%–7.04%. Win rates hovered at ~50%. Critically, the study also tested the practitioner claim that confirming candlesticks with RSI/Stochastics/MFI improves them — it does not.

Don't throw them out, but don't make them a *primary* filter. The right role for pattern detection is as an **entry-timing tiebreaker** on names that already passed a trend/liquidity/catalyst screen, and only after you've measured on your own data whether it helps. Section 10 covers how to test that honestly.

**3. Trend + relative strength is the better foundation.**
For a multi-day-to-multi-week horizon, the most-replicated, easiest-to-automate structure is a Stage-2 uptrend screen. Minervini's Trend Template is the canonical version and it's eight boolean conditions — trivial to compute, no discretion required:

1. Price > 50-day MA
2. Price > 150-day MA
3. Price > 200-day MA
4. 50-day MA > 150-day MA
5. 150-day MA > 200-day MA
6. 200-day MA rising for ≥ 1 month (prefer 4–5 months)
7. Price within 25% of its 52-week high
8. Relative Strength rank > 70 (prefer 90+)

Applied with AND logic across ~4,000 US names this typically leaves 5–20 candidates. That's the shape you want: a small, defensible list your reasoning layer can actually think about.

---

## 2. The architectural rule that matters most

> **The LLM never computes a number. It reads a table it did not produce, and writes prose.**

Every failure mode in AI trading tooling traces back to violating this. Moving averages, RS ranks, ATR, position size, correlation, backtest stats — all of that is deterministic Python, unit-tested, reproducible, cheap. The model's job starts *after* the numbers exist: reconciling the technical setup against the news, spotting the disqualifying catalyst the screen can't see, writing the thesis and the invalidation level, and deciding what to cut.

This gives you three things: the output is auditable (you can re-run the screen and get identical numbers), it's cheap (one or two model calls a day, not thousands), and when something's wrong you can tell whether it was the data, the screen, or the judgment.

### Three layers

```
┌──────────────────────────────────────────────────────┐
│ LAYER 1 — DETERMINISTIC  (plain Python, no LLM)      │
│ ingest → cache → indicators → screen → rank → size   │
│ Output: candidates.json  (5–20 names + full metrics) │
└───────────────────────────┬──────────────────────────┘
                            │
┌───────────────────────────▼──────────────────────────┐
│ LAYER 2 — REASONING  (Claude agents)                  │
│ news reconciliation → thesis → risk veto → editor     │
│ Output: brief.json  (3–5 names, prose, levels)        │
└───────────────────────────┬──────────────────────────┘
                            │
┌───────────────────────────▼──────────────────────────┐
│ LAYER 3 — DELIVERY & MEMORY                           │
│ Telegram push · trade journal · outcome tracking      │
└──────────────────────────────────────────────────────┘
```

---

## 3. Agent roster

Six agents. Four run daily, two run on demand. Each has a hard input/output contract so you can test them in isolation.

### Daily (in the 5 AM pipeline)

**`market-regime`** — *runs first, and can kill the whole brief*
Reads SPY/QQQ vs their 50/200-day MAs, % of S&P constituents above their 200-day, VIX level and 10-day change, and the sector-relative-strength table. Outputs one of `RISK_ON` / `NEUTRAL` / `RISK_OFF` plus a two-sentence rationale.
This agent has veto power: in `RISK_OFF` the brief goes out saying "no new swing entries today" and lists nothing. Roughly 60–70% of swing-strategy drawdown comes from taking good setups in bad tape. Building the off-switch first is the highest-leverage thing in this document.

**`catalyst-analyst`** — *runs per candidate, in parallel*
Input: one ticker + its metrics row. Pulls the last 7 days of ticker-tagged news, the earnings calendar, and any recent 8-K/S-1/424B filings from EDGAR. Outputs: catalyst summary, a `catalyst_quality` score, and a hard `disqualify` boolean.
**Disqualify rules are non-negotiable and mechanical, not judgment calls:** earnings within the holding window, pending secondary offering or ATM, reverse split announced, going-concern language, acquisition target at a fixed price. A chart can look perfect and be uninvestable for reasons only the filings know — this agent is what stops that.

**`thesis-writer`** — *runs once, sees all surviving candidates*
Input: the full surviving set with metrics + catalyst notes. Output: for each of the final 3–5 names — a 2–3 sentence thesis, entry trigger, stop (ATR-derived, from Layer 1), first target, R:R, position size, and **an explicit invalidation condition** ("thesis is dead if it closes below $X or if sector RS drops out of the top 5").
Seeing all candidates at once matters: it can say "these three are the same semiconductor trade, take one."

**`brief-editor`** — *runs last*
Compresses everything into a Telegram-shaped message under ~1,500 characters. Ruthless. If your morning message needs scrolling, you'll stop reading it by week three.

### On demand

**`backtest-engineer`** — you describe a rule change in English; it writes the vectorized implementation, runs the walk-forward harness from §10, and reports honest stats including the ones that make the idea look bad. Never touches the production screen directly — it opens a PR.

**`cost-optimizer` (tokenwise)** — *wraps every other agent, not a separate call*
Not an agent that reasons; a wrapper that every LLM call goes through. Four levers — difficulty routing, prompt-cache breakpoint placement, context compaction, semantic response caching — plus an append-only ledger that bills each request twice: what you actually paid, and what it would have cost unoptimized. Measured on this workload it takes Stage B from **$24.52/mo to $5.49/mo (77.6%)**.

Wired in `pipeline/llm.py` via a single `agent_for(role)` factory so every agent shares one config and one ledger. **The semantic cache is the one component that can silently corrupt a brief** — its 0.93 default threshold would match yesterday's catalyst prompt against today's — so it is disabled for `thesis-writer` and `market-regime`, namespaced per trading day, and tightened to 0.97 with a 6-hour TTL. Full rationale in `docs/cost-control.md`.

**`journal-analyst`** — runs weekly (Sunday). Reads the logged briefs plus your actual fills, and answers: which screen conditions correlated with winners, what did the regime filter save or cost you, where did the LLM's thesis diverge from what happened. This is the only agent that can improve the system over time, and it's the one everyone skips.

---

## 4. Skills to write

Skills are markdown-plus-scripts folders that load on demand. Five worth having:

| Skill | Purpose |
|---|---|
| `swing-screen` | The canonical definition of your screen — every threshold, why it's there, when it was last changed. The single source of truth agents and you both read. |
| `risk-sizing` | Position sizing math (fixed-fractional on ATR stop), max portfolio heat, correlation caps, max positions. Pure formulas so the model can't improvise them. |
| `market-data` | How to call each API, the exact rate limits, the cache-first pattern, retry/backoff, and what to do when a source is down. Stops the model from re-deriving broken API calls every session. |
| `backtest-protocol` | The validation rules from §10 — walk-forward splits, cost assumptions, what constitutes a passing result. Prevents the `backtest-engineer` agent from grading its own homework generously. |
| `brief-format` | Exact Telegram output template, so the morning message is byte-identical in structure every day and skimmable in 20 seconds. |
| `cost-control` | The tokenwise role→tier map and the semantic-cache safety rules, so a later "optimization" can't quietly re-enable cross-day caching. |

Plus a **`CLAUDE.md`** at repo root with data schemas, broker setup, signal definitions, risk rules and library preferences. The most-repeated lesson from people who've put serious hours into Claude Code for trading is that persistent project context is what stops each session from starting cold and re-litigating decisions.

---

## 5. Tools and MCP servers

**Build as an MCP server** (so agents call them directly, no CSV shuffling):

- `market-data-mcp` — `get_ohlcv`, `get_quote`, `get_fundamentals`, `get_earnings_date`. Wraps whichever provider is live, caches to local SQLite/Parquet, enforces rate limits in one place. **Build this first.** Provider churn is the #1 maintenance cost in this kind of system, and this is the seam that contains it.
- `screener-mcp` — `run_screen(config)` → candidate table. Deterministic, testable.
- `portfolio-mcp` — current positions, open risk, sector exposure, portfolio heat. Without this the system recommends names you already own.
- `journal-mcp` — append brief, append fill, query history.

**Install rather than build:** a filesystem MCP for the repo, and optionally SQLite MCP for ad-hoc journal queries.

**Don't build:** an execution/order-placing tool. Not in v1, arguably not ever. The gap between "recommends" and "trades" is where this project either stays useful or becomes expensive.

---

## 6. Data stack

Verified free-tier limits as of August 2026:

| Source | Free tier | Role in this system | Notes |
|---|---|---|---|
| **Alpaca Market Data** | $0 · 200 req/min · 7+ yrs history · corporate actions included | **Primary OHLCV + backtest history** | Free feed is IEX-only, so volume under-reports vs consolidated tape. Fine for daily-bar swing work; scale volume thresholds accordingly. Paper account = free API key. |
| **Finnhub** | $0 · 60 req/min | Company news, earnings calendar, basic fundamentals | Free-tier endpoint coverage shifts — verify current inclusions on their pricing page before depending on any single endpoint. Paid starts ~$80/mo. |
| **SEC EDGAR** | $0 · 10 req/sec | 8-K / S-1 / 424B filings for the disqualify rules | Requires a declared `User-Agent` header (`Name email@domain`) or you get blocked. JSON APIs at data.sec.gov. Filing lag typically 1–3 min. |
| **Tiingo** | $0 · 500 symbols/mo · 50 req/hr · 1,000/day · 30 yrs EOD | Backup EOD source + news (3 mo history) | Free tier too tight for a 4,000-name daily screen; good as fallback. Power tier $30/mo lifts it to 100k req/day. |
| **Alpha Vantage** | $0 · 5 req/min, daily bars only | Fallback only | 5/min is unusable as a primary source. |
| **Marketaux** | $0 · 100 req/day, **3 articles per request** | Ticker-tagged news *with* per-entity sentiment (−1 to 1) | 100 calls/day is fine for 5–20 names, but 3 articles per call is the real constraint — you get headlines, not depth. Basic tier $29/mo lifts it to 2,500/day and 20 articles. |
| **yfinance / Yahoo** | Unofficial | **Avoid in production** | Widely reported as effectively deprecated — unannounced rate limits, unpredictable failures, no SLA. Fine for one-off exploration, not for a job that must run unattended at 5 AM. |
| **IEX Cloud** | — | **Do not use** | Retail API sunset in 2025. |

**Recommended v1 stack: Alpaca (bars) + Finnhub (news/earnings) + EDGAR (filings) + Marketaux (sentiment) = $0/month.** Add Tiingo Power at $30/mo only if you hit a wall on breadth.

Because the daily screen needs ~4,000 symbols' worth of bars, the cache is not optional. Pull the full universe once, then fetch only incremental daily bars. That drops a 4,000-call job to one small delta call per day and keeps you inside every free tier comfortably.

---

## 7. The 5 AM pipeline

Key insight: **for a swing horizon, almost nothing needs to happen at 5 AM.** Your screen runs on prior-day closing data, which is final by ~1:30 PM Pacific the day before. Splitting the job in two makes it faster, cheaper, and far more robust.

**Stage A — 1:00 AM PT (heavy, deterministic, no LLM)**

| Step | Detail |
|---|---|
| Refresh cache | Incremental daily bars for the universe |
| Compute indicators | MAs, RS rank, ATR, 52-wk distance, avg volume, sector RS |
| Run screen | Trend Template + liquidity floor (≥ 400k avg shares, ≥ $5) |
| Rank | Composite score → keep top ~20 |
| Size | ATR-based stops and share counts against current portfolio heat |
| Write | `candidates.json` |

*If Stage A fails, you have four hours to notice and it still ships on time. That margin is the whole reason for the split.*

**Stage B — 5:00 AM PT (light, LLM)**

| Time | Step |
|---|---|
| 5:00 | `market-regime` runs. If `RISK_OFF` → short "stand down" brief, done in 30 seconds. |
| 5:01 | `catalyst-analyst` fans out across the ~20 candidates in parallel; disqualifies apply |
| 5:04 | `thesis-writer` sees survivors, picks final 3–5, writes theses and levels |
| 5:06 | `brief-editor` compresses |
| 5:07 | Telegram push + append to journal |

Market opens 6:30 AM PT, so you have ~80 minutes to read, disagree, and set orders.

**Timezone warning:** cron runs in UTC. 5:00 AM Pacific is **12:00 UTC during PDT** (Mar–Nov) and **13:00 UTC during PST** (Nov–Mar). A fixed UTC cron will silently drift an hour twice a year. Use a scheduler with real timezone support (`TZ=America/Los_Angeles`) or handle the switch explicitly.

---

## 8. Where it runs

| Option | Verdict |
|---|---|
| **Your PC (Task Scheduler / cron)** | Works, free, but only if the machine is reliably awake at 1 AM and 5 AM. Realistically the most common failure mode is "laptop was asleep." |
| **Small VPS** ($5–6/mo) | **Recommended.** Always on, real timezone support, full control, no queue contention. Cheapest reliable answer. |
| **GitHub Actions cron** | Free, but **not for a time-critical job.** Scheduled workflows share the general job queue with no reserved capacity: 5-minute drift is routine, 15 minutes near the top of the hour is common, 30+ minute delays happen on busy days, and community reports include multi-hour delays and outright skipped runs. A 5 AM brief that lands at 5:40 has lost half its usefulness. If you want to use Actions anyway, drive it with `workflow_dispatch` triggered by an external scheduler rather than `on: schedule`. |
| **Cowork scheduled task** (this environment) | Good for the weekly `journal-analyst` review and for interactive research. Less suited to being the hard dependency for a daily unattended job. |

**Runtime for the agents:** the **Claude Agent SDK** (Python or TypeScript) is the right host — it gives you the same agent loop, subagents, MCP support, hooks, permissions and skill loading as Claude Code, but running inside your own process on a schedule. Note that it requires API-key auth; claude.ai subscription rate limits aren't available to SDK-built agents.

---

## 9. Telegram delivery

Setup is 10 minutes: message `@BotFather` → `/newbot` → get token → send your bot a message → read your `chat_id` from `getUpdates` → POST to `/sendMessage`. No library strictly needed, a single `requests.post` works.

Design notes that matter more than the plumbing:

- **One message, under ~1,500 characters.** Use MarkdownV2. If it needs scrolling you'll stop reading it.
- **Send the stand-down message too.** A silent morning is indistinguishable from a crashed job. `RISK_OFF` days should push "No entries — SPY below 50MA, VIX +18% w/w."
- **Add a heartbeat.** If no message arrives by 5:15, something broke. Consider a separate failure alert path so a crash in the main pipeline can still tell you it crashed.
- **Attach one chart per name** as an image — a 6-month daily with the MAs, entry, and stop drawn. This is where you'll catch the LLM being wrong fastest.
- **Reply-to-log:** replying `TOOK AAPL 100 @ 182.50` to the message and having the bot parse it into the journal turns outcome tracking from a chore into a reflex. This is what makes §11's weekly review possible at all.

---

## 10. Validation — the part that decides if any of this is real

This is the section to not rush. A screen that looks great on 2023–2025 data and was tuned on 2023–2025 data tells you nothing.

**Framework:** `backtesting.py` to start (simplest credible option, actively maintained — v0.6.6 shipped July 2026 — clear docs; limited to single-asset but fine for validating individual rules), then `vectorbt` free version if you need large parameter sweeps (note: development has moved to the PRO tier — $25/mo, $240/yr, or $500 lifetime; the open-source version is in maintenance mode). **Avoid `backtrader`** — effectively frozen since ~2023. `PyBroker` is the right pick if you go the ML route, since walk-forward and bootstrapped confidence intervals are built in.

**Non-negotiable rules:**

1. **Walk-forward, never a single split.** Optimize on a rolling 2-year window, test on the following 6 months, roll forward. Report only out-of-sample results.
2. **Fix survivorship bias or state that you haven't.** Screening today's index members over 10 years of history guarantees a flattering result. Either use a point-in-time universe (Zipline Reloaded handles this properly) or write down explicitly that your numbers are optimistic and by roughly how much.
3. **Realistic costs.** Commission plus slippage — for swing entries on liquid names, 0.05–0.10% per side is a defensible assumption. Marginal edges die here, which is exactly what you want to find out before you're trading it.
4. **Pre-register the candlestick test.** Before adding any pattern to the screen: state the hypothesis, run it on out-of-sample data, and accept the result. Given the published evidence, expect no edge — and if your backtest says otherwise, suspect the backtest first.
5. **Track the null.** Log what an equal-weight SPY hold would have returned over the same period, every single time. If the system doesn't clear that after costs and your time, that is a real and useful finding.

**Paper trade for 60 days before a dollar is at risk.** The journal from those 60 days is worth more than any backtest, because it's the only test that includes *you* as a component.

---

## 11. Repo layout

```
stock-recommender/
├── CLAUDE.md                  # persistent project context — schemas, rules, prefs
├── .claude/
│   ├── agents/                # market-regime, catalyst-analyst, thesis-writer,
│   │                          # brief-editor, backtest-engineer, journal-analyst
│   └── skills/                # swing-screen, risk-sizing, market-data,
│                              # backtest-protocol, brief-format
├── mcp/
│   ├── market_data/           # provider abstraction + cache  ← build first
│   ├── screener/
│   ├── portfolio/
│   └── journal/
├── core/
│   ├── indicators.py          # MAs, RS, ATR — pure functions, unit tested
│   ├── screen.py              # Trend Template, thresholds from config
│   ├── ranking.py
│   └── sizing.py
├── pipeline/
│   ├── stage_a_nightly.py     # 1:00 AM
│   └── stage_b_brief.py       # 5:00 AM
├── backtest/
├── data/                      # SQLite/Parquet cache — gitignored
├── journal/                   # briefs + fills + outcomes
└── config/screen.yaml         # every threshold, versioned
```

Put every threshold in `config/screen.yaml` and version it. When the weekly review says "loosen the 52-week-high band to 30%," you want a diff, not an archaeology project.

---

## 12. Build order

**Week 1 — the boring foundation.** `market-data-mcp` with caching and rate limiting. Indicators with unit tests against hand-computed values. Universe definition. *Success: one command pulls 4,000 symbols' bars and computes indicators in under 2 minutes from warm cache.*

**Week 2 — the screen.** Trend Template in code. Ranking. ATR sizing. `candidates.json`. Run it manually for 5 days and eyeball the names — if the list is garbage, no amount of LLM reasoning downstream fixes it. *Success: 5–20 names daily that you look at and think "yeah, those are the right kind of stocks."*

**Week 3 — reasoning and delivery.** The four daily agents. Telegram bot. Journal writes. Run the full pipeline manually at 5 AM for a week before automating it. *Success: a brief you'd actually read.*

**Week 4 — automation and validation.** VPS deploy, both cron stages, heartbeat monitoring. Start the walk-forward backtest and the 60-day paper trade in parallel. Set up the weekly `journal-analyst`.

**Explicitly deferred:** live execution, options, intraday, ML ranking, multi-strategy. Each is a real project. None belongs in v1.

---

## 13. Cost

| Item | Monthly |
|---|---|
| Market data (Alpaca + Finnhub + EDGAR + Marketaux free tiers) | $0 |
| Hetzner CX22 + IPv4 | ~$5.20 |
| Claude API — Stage B, 22 calls/day, **measured 2026-08-20** | **$1.66** (≈$2.45 once news is connected) |
| Telegram | $0 |
| **Total** | **~$7.00** |

**Measured on the first live run, 2026-08-20**, not estimated: 22 requests, $0.079 actual against $0.173 unoptimized — 54.3% saved. Routing accounts for **99.9%** of that; 21 of 22 calls went to Haiku while the single Opus thesis call was 70% of the bill on its own.

Two corrections to the earlier projection, both about prompt *size* rather than routing. Each catalyst call measured ~406 input tokens against an assumed ~7,000, because the news feed was not connected — expect ~$2.45/mo once it is. And `prompt_cache` saved exactly $0: Anthropic needs a 2048-token minimum block on Haiku 4.5, and the whole prompt is a fifth of that, so the lever cannot engage. That is correct behaviour, not a defect, and padding the prompt to reach the threshold would cost more than it saved. See `docs/cost-control.md` §2.

Cost levers, in order: keep Layer 1 fully deterministic (still the largest single saving), cap candidates at 20 before the LLM sees anything, route by role, and let prompt caching carry the shared prefix.

---

## 14. Risks and failure modes

| Risk | Mitigation |
|---|---|
| **Silent failure** — job dies, you assume "no setups today" | Heartbeat message on every run including empty ones; separate alert channel for crashes |
| **Stale cache** — screening on last week's prices | Assert most recent bar date == previous trading day; hard-fail if not |
| **Data provider disappears** | The whole point of `market-data-mcp` — swap providers in one file |
| **Overfitting** | Walk-forward only, pre-registered tests, always report the SPY null |
| **Survivorship bias** | Point-in-time universe, or an explicit written caveat |
| **LLM fabricates a fact** | Model never computes numbers; every claim in the thesis must trace to a field in `candidates.json` or a cited news item |
| **Automation bias** — you stop thinking because it looks authoritative | Every brief carries the invalidation condition and a confidence level; you place every order manually |
| **Timezone drift** | Real TZ-aware scheduling, not a fixed UTC cron |
| **Concentration** | `portfolio-mcp` correlation cap; `thesis-writer` explicitly instructed to collapse duplicate sector trades |

**Compliance, briefly:** this is a personal tool. The moment you share the output with anyone else, US investment-adviser rules potentially attach. Keep it private, or talk to a lawyer before you don't. And every data provider's terms restrict redistribution — Tiingo and Alpaca both do.

*Nothing here is investment advice, and I'm not a financial adviser. Swing trading loses money for most people who try it; a well-engineered pipeline changes the quality of your decisions, not the odds of the underlying activity.*

---

## 15. Decisions — resolved

| # | Decision | Consequence |
|---|---|---|
| 1 | **S&P 500** universe | ~500 symbols, so the whole daily pull sits inside every free tier with room to spare. Cleanest possible data, no junk to filter. Expect fewer candidates than a broad screen — some days legitimately return zero. |
| 2 | **Strict 8-of-8** Trend Template | An empty list is a real answer, not a bug. Do not add a "score-based" fallback later just because a week goes quiet — that's the exact pressure the strict rule exists to resist. |
| 3 | **No fundamental overlay in v1** | Saves the Finnhub fundamentals dependency entirely. Revisit only after the journal has enough fills to test whether EPS/revenue filters would have improved outcomes. |
| 4 | **Telegram reply-to-log** | No broker API, no OAuth, no credentials beyond what you already have. `TOOK AAPL 100 @ 182.50` as a reply. Build in week 3 — the weekly review is impossible without it. |
| 5 | **Hetzner CX22** | ~$5.20/mo, 2 vCPU / 4 GB. Server timezone set to `America/Los_Angeles` so systemd timers are local-time and DST-correct with no UTC arithmetic. |

### Still open

- **Alpaca's IEX-only free feed under-reports volume.** The 400k-share liquidity floor is calibrated for consolidated tape. On S&P 500 names this is unlikely to bind, but confirm against a few known-liquid tickers in week 1 before trusting the filter.
- **`catalyst-analyst` disqualify rules need a written spec** before coding — they're the only place a mechanical rule prevents an expensive mistake.
- **Chart images in the brief** (§9) add a matplotlib render step to Stage B. Worth it, but decide before week 3 whether it's v1 or v2.

---

## 16. What exists now

| Path | State |
|---|---|
| `docs/setup-telegram.md` | Complete walkthrough — BotFather, chat ID, verification, failure modes |
| `docs/setup-vps.md` | Complete walkthrough — Hetzner provisioning, hardening, timers, operations |
| `docs/cost-control.md` | tokenwise reconstruction, wiring, measured savings, the semantic-cache hazard |
| `notify/telegram.py` | Working. MarkdownV2 escaping, chunking, 429 backoff, plain-text fallback, alert path |
| `notify/test_telegram.py` | 22 tests, all passing |
| `notify/send_test.py` | End-to-end Telegram verification |
| `pipeline/llm.py` | tokenwise integration — `agent_for(role)` factory |
| `pipeline/test_llm.py` | 6 tests, all passing |
| `vendor/tokenwise/` | Reconstructed from the shuffled upload; imports clean, simulation runs |
| `deploy/bootstrap.sh` | Idempotent VPS provisioning |
| `deploy/install-timers.sh` | Timer installation |
| `deploy/deploy.sh` | rsync + pip deploy |
| `deploy/systemd/*` | 3 services + 3 timers, verified with `systemd-analyze verify` |
| `core/indicators.py` | SMA, Wilder ATR, IBD-style RS score + cross-sectional rank, 52-wk range, MA slope, liquidity. Lookahead guard included. |
| `core/screen.py` | Trend Template, strict 8-of-8 + 2 gates. Per-condition columns, rejection funnel, first-failure attribution. |
| `core/ranking.py` | Weighted percentile composite, deterministic ordering, capped at `max_candidates` |
| `core/synthetic.py` | Price generators with known properties — one per branch of the screen |
| `core/test_screen.py` | 61 tests, all passing. Indicators checked against hand-computed values. |
| `core/demo.py` | End-to-end run on a synthetic universe |
| `docs/screen.md` | Screen documentation and three findings from building it |
| `core/sizing.py` | ATR stops, R targets, four independent caps (risk / notional / liquidity / heat), sector concentration |
| `core/test_sizing.py` | 27 tests |
| `mcp/market_data/cache.py` | SQLite bar cache, incremental refresh, split detection |
| `mcp/market_data/provider.py` | Alpaca + offline providers, `BarBundle`, staleness assertion |
| `mcp/market_data/universe.py` | S&P 500 loader, weekly CSV cache, survivorship warning |
| `mcp/market_data/test_market_data.py` | 26 tests |
| `pipeline/stage_a_nightly.py` | The 01:00 job. Runs end to end offline. |
| `pipeline/watchdog.py` | 05:20 check that a brief was actually delivered |
| `pipeline/notify_failure.py` | systemd `ExecStopPost` alert, silent on success |
| `pipeline/test_stage_a.py` | 18 contract tests on `candidates.json` |
| `docs/stage-a.md` | Nightly job, sizing caps, IEX volume problem |
| `core/regime.py` | Deterministic regime verdict — the off-switch. Config validated for reachability. |
| `core/test_regime.py` | 24 tests |
| `mcp/news/providers.py` | Finnhub news + earnings, EDGAR filings, offline provider |
| `mcp/news/disqualify.py` | Eight mechanical disqualify rules |
| `mcp/news/test_news.py` | 21 tests |
| `mcp/journal/` | Append-only brief + fill journal, strict reply parser |
| `pipeline/agents/base.py` | JSON contract: extraction, schema, one repair retry, offline stub |
| `pipeline/agents/catalyst.py` | Per-name catalyst read, parallel fan-out |
| `pipeline/agents/thesis.py` | One call over all survivors — the only place concentration is visible |
| `pipeline/agents/editor.py` | Deterministic Telegram renderer + one-line market summary |
| `pipeline/stage_b_brief.py` | The 05:00 job. Runs end to end offline. |
| `pipeline/test_stage_b.py` | 54 tests |
| `docs/stage-b.md` | The brief, the off-switch, disqualify rules, the journal |
| `pipeline/fill_listener.py` | Reply-to-log poller with persisted offset and at-least-once dedupe |
| `pipeline/weekly_review.py` | journal-analyst — joins briefs to fills, FIFO round-trips, regime cost |
| `backtest/panels.py` | Date x symbol indicator matrices built from the production functions |
| `backtest/engine.py` | Event loop, next-open fills, gap-aware stops, realistic costs |
| `backtest/walkforward.py` | Rolling train/test, out-of-sample-only reporting, overfitting gap |
| `backtest/metrics.py` | CAGR, drawdown, exposure, expectancy, and the SPY null every time |
| `backtest/test_backtest.py` | 37 tests including the panel/production identity check |
| `docs/backtest.md` | The validation protocol |
| `CLAUDE.md` | Persistent project context |
| `.claude/skills/` | 6 skills: swing-screen, risk-sizing, market-data, backtest-protocol, brief-format, cost-control |
| `core/exits.py` | Sell rules — stop/target/time/trend/RS, ratcheting stops, scale-out |
| `core/test_exits.py` | 27 tests |
| `paper/account.py` | Paper account: weekly deposits that carry over, fractional shares |
| `paper/broker.py` | Paper execution, same timing model as the backtest |
| `paper/run.py` | The $100/week one-month test, plus `--scan` over every window |
| `paper/test_paper.py` | 23 tests |
| `pipeline/positions.py` | Live position tracking and the daily sell check |
| `docs/paper-trading.md` | Position tracking, exit rules, the paper test |
| `pipeline/preflight.py` | Credential, volume-scale, universe and dry-run checks before going live |
| `pipeline/test_preflight.py` | 14 tests |
| `docs/launch-checklist.md` | The ordered path from here to real money |
| `run_tests.sh` | All 388 tests, one command |
| `.env.example`, `.gitignore`, `requirements.txt` | Ready |

**Not built yet** — chart images in the brief, and a point-in-time universe for honest backtests. Everything else in §3, §4 and §10 exists and runs offline, plus position tracking and a paper-money test that were not in the original design. Nothing has touched real Alpaca bars yet.

### Findings that change the design

Building the Trend Template surfaced three things worth recording here, not just in `docs/screen.md`:

1. **Conditions 2 and 3 are logically implied by the others.** c1 ∧ c4 ⟹ c2, and c2 ∧ c5 ⟹ c3. The eight conditions are six independent constraints plus two that can never fail *alone*. A "6 of 8" scored variant would triple-count the moving-average stack. **Corrected 2026-08-21:** I originally claimed the funnel would show these eliminating zero names, and the first live run showed c2 eliminating 46 and c3 eliminating 10. The proof is fine; the claim about the *funnel* was not. Conditions are evaluated in Minervini's order, so c2 is tested before c4 and catches names c4 would have caught — the passing set is unchanged, only the attribution moves. Synthetic data hid it because monotonic series never produce `close > MA50` with `MA50 < MA150`. The funnel now reports a `sole` column (names only that condition rejects), which is zero for c2 and c3 as the proof requires.

2. **Condition 7 earns its place, but narrowly.** Across ~600 smooth drawdown-and-recovery shapes, nothing fails c7 alone — a name 25%+ off its high has always broken its MA structure too. The one shape that isolates c7 is a **short isolated spike** (squeeze, buyout rumour, index add) that sets a high the stock never revisits while barely moving the 200-day. That's a real and specific class of bad buy, and without c7 the screen takes it.

3. **RS rank is cross-sectional, so the universe is itself a screen parameter.** `min_rs_rank: 70` admits roughly the top 30% of whatever is fed in. Changing the universe re-ranks every existing name even with `config/screen.yaml` untouched — so the universe definition must be versioned alongside the thresholds, and a universe change must be treated as a screen change when backtesting.

4. **A default value flowing into a grouping key turns a missing feed into a silent policy change.** With no sector data every candidate became `UNKNOWN`, and the `max_same_sector: 2` cap then rejected everything after the second name — reporting a reason that was not true and quietly taking 2 positions instead of 6. The concentration cap is now skipped when the sector is unknown, and `sectors_unknown` surfaces the missing protection. Worth watching for wherever else a default meets a group-by.

5. **`candidates.json` must be internally consistent, not merely accurate.** Prices serialised at four decimals while risk came from full-precision internals, so `shares x (entry - stop)` did not reproduce the file's own risk figure. Off by under a cent, and corrosive: a brief whose arithmetic does not tie is one you start double-checking and then stop reading. Order prices now serialise at two decimals and every derived figure comes from those.

7. **The off-switch must not be a model output.** §3 originally had a `market-regime` agent produce the RISK_ON / RISK_OFF label. That is the wrong shape for a component whose whole job is to say no: a model asked "is the tape healthy?" will occasionally say yes on a day the rules say no, and say it persuasively. The verdict is now computed in `core/regime.py` from config thresholds; the model writes the sentence that explains it. `RegimeConfig.validate()` additionally refuses a config where the NEUTRAL band is stricter than the RISK_OFF band, which would make RISK_OFF unreachable — a broken off-switch that looks like a working one.

8. **The brief is rendered, not composed.** §3's `brief-editor` was to "compress everything into a Telegram-shaped message". That hands a model three jobs it fails silently at: valid MarkdownV2 (one unescaped `.` and Telegram rejects the whole message), staying under the length cap, and not altering a number in transit. `thesis-writer` already caps theses at two sentences, so little compression remained — only risk. Layout is now code, escaping goes through `escape_md`, length is enforced by construction, and the model writes only the one-line market summary.

9. **The regime benchmark is not an index constituent.** SPY is an ETF, so it was never in the fetch list. Stage B would have computed the regime with no benchmark, degraded to NEUTRAL every single day, and rendered RISK_OFF unreachable — the same broken-off-switch failure as finding 7, arrived at from the opposite direction. Stage A now appends the benchmark explicitly and drops it before screening, and the offline path includes a synthetic SPY so the regime code is exercised on every run.

11. **A backtest fixture of independent stocks is not a market.** The first offline backtest returned *zero trades over six years*: with one-third uptrends, one-third downtrends and one-third flat, breadth sat at ~33% every day, below the RISK_OFF threshold. The filter was right; the fixture was wrong. `synthetic.market()` now generates a single-factor model — one market series with bull/bear phases, plus per-symbol beta and idiosyncratic noise — which produces breadth that swings and correlation that makes the sector cap and clustered drawdowns real.

12. **When you duplicate a rule for performance, duplicate the input, never the decision.** The engine originally reproduced the regime thresholds locally and passed an empty universe to `assess()` to avoid recomputing 500 rolling means per simulated day. Breadth came back NaN, which degraded the reading, which floored the verdict at NEUTRAL — so **RISK_ON was unreachable and every backtest ran at half risk**, with nothing looking wrong. `assess()` now takes an optional precomputed `breadth`; `core/regime.py` still owns every threshold.

13. **The walk-forward caught its own overfitting, which is the point.** On synthetic data with no real edge to find, a 12-window run reported an overfitting gap of **+0.550R** (in-sample minus out-of-sample expectancy) and flagged it automatically. The per-window breakdown showed the signature: five of twelve windows produced 0-3 trades, two were negative. This is the harness working — a single-split run over the same data looked fine.

19. **A fix that writes config nothing reads is worse than no fix.** `preflight --fix-volume-scale` wrote `universe.volume_scale` into `config/screen.yaml`, and `AlpacaProvider` — whose constructor took `volume_scale` as an argument defaulting to 1.0 — never looked at it. The command would have reported success and changed nothing, which is the failure mode hardest to notice. `resolve_provider` now reads the config value.

15. **Exits deserved as much design as entries, and had none.** The original design covered screening in detail and said nothing about selling. `core/exits.py` closes that gap with the same governing rule: deterministic, model-free. Two decisions carry most of the weight — the stop is checked against the day's **low**, not the close (a position that traded through its stop is out, whatever it closed at), and stops **only ratchet up** (a rule that could lower a stop would widen risk on exactly the positions that had started working).

16. **A single month is a draw, not a measurement.** Running every four-week window over the synthetic history: 34 windows, **17 of which never traded at all** because the regime filter stood the account down for the entire month, best month +$14.31 and worst −$3.06 on $400 deposited. A one-month paper test is worth running for what it *does* prove — that the machinery runs unattended, the exits fire at sane prices, and the account does not get stuck — and worth nothing at all as evidence of edge.

17. **Small accounts break fixed-fractional sizing.** At $400 equity and 0.75% risk, whole-share sizing returns zero shares on any stock above about $20 — the paper account would have sat in cash for a month proving nothing. Fractional shares were added to `SizingConfig` rather than forked into a parallel paper-only path, so paper and production share one sizing implementation.

18. **R must be persisted, not re-derived.** `ClosedTrade` originally omitted `risk_per_share` from its serialisation and fell back to a 5%-of-entry estimate on load, which **doubled every R-multiple after a restart**. R is anchored to the risk actually taken at entry and cannot be recovered from entry and exit prices alone.

14. **RISK_OFF blocks new entries; it does not close open positions.** Exposure ran 85% of days against 1,070 RISK_OFF days, which looks contradictory until you notice positions opened in RISK_ON carry through. That is deliberate — stops manage exits, and force-liquidating on a regime flip would turn a filter into a whipsaw generator — but it means the regime filter's effect shows up in *entries avoided*, not in time out of the market.

10. **`{` in prose breaks naive JSON extraction.** The obvious heuristic — first `{` to last `}` — produces an invalid span the moment a model writes "note {x}" before its payload. Replaced with `json.JSONDecoder().raw_decode` scanning from every opening bracket, which also handles braces inside string literals correctly.

6. **"Stale" means the previous *session*, not the previous *day*.** Stage A runs at 01:00, so the newest complete session is always the previous business day; treating Thursday's close as stale on Friday morning would refetch the universe every morning for nothing. The check ignores market holidays on purpose — a wasted API call is cheaper than screening on old prices.

---

## Sources

- Humbled Trader, *I Built an AI Trading System With Claude + TradingView* — https://www.youtube.com/watch?v=IqvnryFzZD4
- Humbled Trader, *I Replaced my 6AM Premarket Trading Routine with Claude + Codex* — https://www.youtube.com/watch?v=PKFkJ4TprVo
- TradingLab, *DAY TRADING Explained in 11 Minutes* — https://www.youtube.com/watch?v=edfHZq5lrTA
- TradingLab, *The ONLY Candlestick Pattern Guide You'll EVER NEED* — https://www.youtube.com/watch?v=tW13N4Hll88
- Tharavanij, Siraprapasiri & Rajchamaha, *Profitability of Candlestick Charting Patterns in the Stock Exchange of Thailand*, SAGE Open 2017 — https://journals.sagepub.com/doi/10.1177/2158244017736799
- Minervini Trend Template criteria — https://www.finermarketpoints.com/post/mark-minervini-s-stock-screener-what-indicators-and-criteria-does-he-use
- Alpaca market data plans — https://alpaca.markets/data
- Tiingo pricing — https://www.tiingo.com/about/pricing
- SEC EDGAR access rules — https://www.sec.gov/os/webmaster-faq
- Free stock API comparison (2026 tested) — https://thenextgennexus.com/2026/05/15/10-best-free-stock-market-apis-2026/
- Financial news sentiment API comparison — https://adanos.org/insights/blog/best-financial-news-sentiment-apis-2026/
- Python backtesting frameworks compared — https://quanttradingtools.com/python-backtesting-frameworks/
- backtesting.py releases — https://pypi.org/project/backtesting/
- VectorBT PRO membership — https://vectorbt.pro/become-a-member/
- Marketaux pricing — https://www.marketaux.com/pricing
- GitHub Actions cron drift — https://crontap.com/blog/github-actions-cron-drift-problem
- Claude Agent SDK overview — https://code.claude.com/docs/en/agent-sdk/overview
- *900+ Hours of Using Claude Code for Trading* — https://aiintrading.substack.com/p/claude-code-trading-900-hours
