# Talking about this project in interviews

~9,000 lines of Python, 3,300 lines of tests, 388 tests, 12 design documents.

**Lead with the engineering problem, not the trading.** "I built a stock
picker" invites scepticism and a conversation about whether you beat the
market. "I built a system where an LLM writes prose and Python does arithmetic,
and the interesting work was deciding which is which" is an engineering
conversation, which is the one you want.

---

## The 30-second version

> It's an automated swing-trading research pipeline. Every weekday it screens
> the S&P 500 against a technical trend model, checks news and SEC filings for
> disqualifying events, sizes positions against a risk budget, and pushes a
> brief to Telegram before the market opens.
>
> The design rule that shaped everything: **the language model never computes a
> number.** All the maths is deterministic, unit-tested Python. The model reads
> a table it didn't produce and writes the reasoning. That made it auditable,
> reproducible, and about $2.70 a month to run.

Stop there. Let them ask.

---

## The 2-minute version

> Two stages. At 1am a deterministic job refreshes a bar cache, computes
> indicators, runs an eight-condition trend screen, ranks the survivors, and
> sizes them against four separate caps — per-trade risk, position notional,
> liquidity, and total portfolio heat. It writes a JSON file.
>
> At 5am a second stage picks that file up. It computes a market-regime verdict
> — which can veto the whole thing — pulls news and filings for the ~20
> survivors, applies mechanical disqualification rules, and only then hands the
> table to the model. One cheap model call per candidate for the catalyst read,
> one expensive call over all of them together for the theses, and a
> deterministic renderer for the message itself.
>
> It runs offline end to end with synthetic data, so I could develop and test
> the whole thing before I had a single API key. And it has a backtest harness
> with walk-forward validation that reports its own overfitting gap.

---

## The three decisions worth talking about

These are the parts that show judgment rather than effort. Pick whichever fits
the conversation.

### 1. I removed the LLM from two places I'd originally designed it into

**The market regime verdict.** The original design had an agent decide whether
the market was healthy enough to trade. I changed it to fixed rules in Python.

> A model asked "does the market look OK?" every single morning will eventually
> say yes on a day the rules say no — and it will say it persuasively. The one
> component you can't afford to be argued out of is the off-switch. So the
> rules decide, and the model writes the sentence explaining the verdict, which
> is the thing it's actually better at.

**The brief rendering.** The original design had the model compose the Telegram
message.

> That hands a model three jobs it fails silently at: emitting valid
> MarkdownV2 — one unescaped period and Telegram rejects the whole message —
> staying under a length cap, and not altering a number in transit. The first
> two are formatting bugs that only appear at 5am. The third is a correctness
> bug you might never notice.

**The general principle**, if they push: *the interesting question in an LLM
system isn't what you can make the model do — it's what you should stop it from
doing.*

### 2. Real data disproved something I'd proven and documented

I'd shown mathematically that two of the eight screen conditions are logically
redundant — condition 1 and condition 4 together imply condition 2 — and
documented that they'd never eliminate anything. Synthetic tests agreed.

First live run, condition 2 eliminated 46 stocks.

> The proof was right. My claim about the *diagnostic* was wrong. Conditions are
> evaluated in a fixed order, so condition 2 is tested before condition 4 and
> catches names condition 4 would have caught — the passing set is identical,
> only the attribution moves. Synthetic data hid it because smooth generated
> price series never produce the configuration that triggers it. Real
> recovering stocks do it constantly.
>
> I added a second column to the output distinguishing "eliminated in sequence"
> from "eliminated uniquely", corrected the docs, and wrote a test asserting the
> second is always zero.

**Why this is good interview material:** it shows you can hold "my proof is
correct" and "my documentation is wrong" simultaneously, and that you go back
and fix the record.

### 3. The bug where a safety check destroyed the thing it was checking

I wrote a preflight script to validate credentials before a live run. It
dry-ran both stages to confirm they worked — in-process, which meant it wrote
synthetic test candidates straight over the real output file.

> The next morning's brief analysed four stocks that don't exist. Correct
> reasoning, real-looking rating scores, coherent sector analysis — of
> fictional tickers. The only tell was the ticker names.
>
> That's the worst failure class in the system: a check that silently destroys
> its own subject, and produces output plausible enough to act on. I moved the
> dry run into a subprocess with an isolated data directory — the only version
> that genuinely can't reach live state — and added a regression test that
> writes a sentinel value and fails if it disappears.

---

## If they ask about testing

> 388 tests. The discipline I'd defend is that every indicator is checked
> against values I computed by hand in the docstring, not against what the code
> currently returns. A test that records current behaviour catches nothing.
>
> The most important single test asserts that the backtest's vectorized
> indicator panels are numerically identical to the production functions, to
> nine decimal places, on random symbol/date pairs. A backtest that
> reimplements the strategy is testing the reimplementation. If that test fails,
> nothing else in the backtest means anything.

## If they ask about cost

> $2.70 a month, measured rather than estimated. 54% saved against an
> unoptimised baseline, and routing accounts for 99.9% of that — 21 of 22 daily
> calls go to the cheap model, and the single expensive call is 70% of the bill.
>
> The interesting part is that prompt caching saved exactly zero. There's a
> 2048-token minimum block size and my prompts are 400 tokens, so the lever
> can't engage. Knowing *why* a lever contributes nothing matters — the wrong
> response would have been padding the prompt to reach the threshold, which
> costs more than it saves.

## If they ask whether it makes money

Be straight. This is a credibility test.

> I don't know, and I've worked out that I can't know yet. At six trades a
> month with the observed variance, proving a real edge at 80% power takes
> about 87 trades — roughly 14 months. Three good months is noise. So I gated
> going live on *process* criteria, which are measurable in weeks — did it run
> every day, did I actually read it, did I accept the stand-down days — and on
> sizing small enough that being wrong is affordable.
>
> The backtest reports buy-and-hold on an index fund alongside every result, on
> purpose. If it can't beat that, the honest conclusion is to stop.

**This answer is stronger than a good return would be.** It demonstrates you
understand statistical power, and that you'd rather be right than impressive.

---

## The stack — what, why, and the gotcha

Interviewers ask "what did you use." The useful answer includes why you chose
it and what bit you, because that's what separates having used something from
having read about it.

### Market and reference data

**Alpaca Markets** — commission-free US broker with a free market-data API.
Used for daily OHLCV bars across the S&P 500. Chosen because the free tier
gives 200 requests/minute and 7+ years of history with no funding requirement,
which nothing else matches.

> **Gotcha worth telling:** the free tier is IEX-only — a single exchange with
> a low-single-digit share of consolidated volume. My liquidity filter was
> calibrated for full-market volume, so on live data it would have rejected
> essentially the entire index and returned an empty list every morning that
> looked exactly like a quiet market. I measured it at **3.5% of consolidated
> tape** with a 3.2x spread across symbols, and recalibrated the threshold to
> the feed's own units rather than scaling the volume up — scaling would have
> put an invented number in the output and was only accurate to a factor of two.

**Finnhub** — financial data API. Used for company news and the earnings
calendar. The earnings dates matter more than the news: they drive a hard
disqualification rule, because a perfect chart with earnings in three days is a
coin flip, not a setup.

**SEC EDGAR** — the SEC's own filing database, free, no key. Used to detect
8-K material events, S-1/S-3/424B registrations (dilution incoming) and SC 13E3
(going private).

> **Gotcha:** EDGAR blocks automated access that doesn't declare a User-Agent
> with a real contact address. It fails silently rather than erroring clearly.

**Wikipedia** — the S&P 500 constituent list, scraped weekly and cached.

> **Gotcha:** `pandas.read_html(url)` uses urllib's default User-Agent, which
> Wikipedia rejects with a bare HTTP 403 — reads like a firewall problem and
> isn't one. Fetching via `requests` with a descriptive header fixes it. I also
> added the MediaWiki API as a fallback, since it's built for automation.

### AI

**Anthropic Claude API** — three models, routed by task difficulty. Haiku 4.5
for the ~20 per-stock catalyst reads, Opus 5 for the single synthesis call that
sees every candidate together, Sonnet 5 available as a middle tier. Routing is
the entire cost story: 21 of 22 daily calls go to the cheap model, while the one
expensive call is 70% of the bill — and it's the call worth paying for.

**Claude Code** — I built the project with it. Worth being upfront about; the
interesting answer is what I *didn't* delegate: the architecture decisions, the
choice to remove the model from the regime verdict and the rendering, and every
threshold in the config.

### Delivery

**Telegram Bot API** — free, no infrastructure, and it's already on my phone.
Two-way, which is the part that matters: replying `TOOK PSX 14 @ 242.50` to the
brief logs a fill, which is what makes the weekly review possible at all.

> **Gotcha:** MarkdownV2 requires escaping sixteen reserved characters. One
> unescaped period and the API rejects the entire message. This is a large part
> of why I made the renderer deterministic rather than model-generated.

### Python

| Library | Used for |
|---|---|
| **pandas / numpy** | All indicator maths — rolling windows, cross-sectional ranking, the date × symbol panels the backtest runs on |
| **requests** | Every HTTP call, so timeouts and retries are handled in one place |
| **alpaca-py** | Alpaca's official SDK |
| **anthropic** | Claude's official SDK |
| **sqlite3** (stdlib) | The bar cache. Chosen over Parquet because the access pattern is "one symbol since date X" — an index lookup, not a columnar scan — and it gives transactional writes free |
| **PyYAML** | Config. Every threshold lives in one versioned YAML file; a strategy change is a diff |
| **lxml / BeautifulSoup** | HTML parsing for the constituent list |
| **unittest** (stdlib) | 388 tests. No pytest — nothing needed it |

### Infrastructure

**Hetzner Cloud** — €4/month VPS. Chosen over AWS/GCP because this needs 2
vCPUs and reliable timing, not elasticity.

**systemd timers** — over cron, for three reasons: `Persistent=true` means a
missed run fires when the machine returns rather than silently vanishing;
`OnCalendar` is timezone-aware once the host timezone is set, so no UTC
arithmetic and no twice-yearly DST drift; and failures are visible through
`systemctl status` and the journal.

> **Gotcha:** `StartLimitBurst` and `StartLimitIntervalSec` must go in the
> `[Unit]` section. systemd silently ignores them in `[Service]`, so your retry
> cap does nothing and you never find out.

**ufw / fail2ban / unattended-upgrades** — standard hardening. Service units
run with `ProtectSystem=strict` and an explicit three-directory write allowlist.

### Methods, and where they came from

**Minervini Trend Template** — an eight-condition Stage-2 uptrend screen from
Mark Minervini's books. Chosen because it's fully mechanical: eight booleans,
no discretion, trivially testable.

**Wilder's ATR** — J. Welles Wilder's Average True Range, used for volatility-
scaled stops. Implemented with his original smoothing rather than a rolling
mean, because a rolling mean drops the bar leaving the window and ATR jumps
when an old outlier expires — which would show up as position sizes lurching
for reasons unrelated to the stock.

**IBD-style relative strength** — weighted trailing return, most recent quarter
double-weighted, percentile-ranked across the universe.

**Walk-forward validation** — standard quantitative practice: optimise on a
rolling window, test on the period after it, report only out-of-sample results.

### My own

**tokenwise** — a cost layer I wrote separately and vendored in. Wraps the
Claude client with difficulty routing, prompt-cache breakpoint placement,
context compaction and a semantic response cache, and bills every request twice
— actual and counterfactual — so the savings figure is an audit rather than a
claim.

> **The best story here:** its semantic cache defaults to a 0.93 similarity
> threshold with a 24-hour TTL. In a daily financial pipeline that's a trap —
> Monday's prompt for a stock and Tuesday's differ only in a few prices, so
> similarity sits well above 0.93. It would have served yesterday's analysis
> for today's setup and *recorded it as a saving*: a better cost number for a
> worse output, which is the hardest kind of bug to notice. I namespaced the
> cache per trading day, raised the threshold, and disabled it entirely for the
> two agents where a stale answer would be most damaging.


---

## Likely follow-ups

**"Why not use an off-the-shelf backtesting library?"**
> I used `backtesting.py` conventions but wrote the engine, because I needed
> the backtest to call the exact same screening functions production calls.
> Every library I looked at wanted me to reimplement the strategy in its own
> idiom, which reintroduces the bug the backtest exists to catch.

**"How do you know the LLM isn't hallucinating numbers?"**
> Structurally it can't affect them. Numbers are computed upstream and
> interpolated into the message by code — the model never touches them. What it
> *can* do is misattribute meaning to one, and it did: it described a position
> that was mechanically trimmed by the portfolio heat cap as "the right size for
> a moderating story." Nothing false, but intent attributed to a number it
> didn't choose. I fixed it by passing the binding constraint into the prompt
> and instructing it explicitly that size carries no information.

**"What would you do differently?"**
> Test against real data much earlier. Everything I got wrong — a
> platform-specific path default, a mis-scaled volume threshold, that
> redundancy claim — was invisible under synthetic fixtures and obvious within
> minutes of live data. My synthetic generators were too clean; I eventually had
> to rebuild the market fixture as a correlated single-factor model with
> bull/bear phases, because independent random walks never produce the
> conditions that matter.

**"Did you use AI to build it?"**
> Yes, Claude Code, and I'd say the same thing about it that the project itself
> argues: the value was in deciding what *not* to delegate. The architecture
> decisions, the choice to pull the model out of the regime verdict and the
> renderer, every threshold in the config, and the call that a month of paper
> results can't validate anything — those are mine, and they're the parts that
> determine whether the system is any good.

**"Is this production?"**
> It runs on a €4 VPS on systemd timers with a watchdog that alerts if the brief
> doesn't arrive. Every component has a documented degraded mode and the brief
> is flagged when one fires. Two things abort rather than degrade: stale price
> data and a stale candidate file, because both produce output that looks
> completely normal and is completely wrong.

---

## Don't claim

- That it beats the market — you don't know
- That the backtest validates anything — it ran on synthetic data
- "AI-powered trading" as a headline — it invites the wrong conversation
- Any specific return number

## Do claim

- Deliberate, defensible architecture with a stated governing principle
- Genuine test discipline, including tests that caught your own errors
- Measured costs and measured savings, with an explanation of why one lever failed
- Statistical literacy about what your own results can and can't show
- That you corrected the record when data contradicted you

---

## The line to close on

> The most useful thing I learned wasn't about markets. It's that in an LLM
> system, the hard engineering decisions are about what to *withhold* from the
> model — and that the failure modes worth designing against are the ones that
> produce confident, plausible, wrong output rather than an error.
