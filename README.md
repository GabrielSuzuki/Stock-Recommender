# stock-recommender

Swing-trade brief pushed to Telegram every weekday at 05:00 Pacific.

**Architecture:** `architecture.md` — read this first.

## Start here

```bash
pip install -r requirements.txt
cp .env.example .env          # add your Alpaca keys at minimum
python3 -m pipeline.preflight # tells you exactly what is blocking
```

`docs/launch-checklist.md` is the ordered path from here to real money.

## Setup, in order

1. `docs/setup-telegram.md` — bot, chat ID, verification (~10 min)
2. `docs/setup-vps.md` — Hetzner CX22, hardening, systemd timers (~35 min)
3. `docs/cost-control.md` — tokenwise wiring and the semantic-cache hazard
4. `docs/screen.md` — the Trend Template implementation and what building it revealed
5. `docs/stage-a.md` — the nightly job, sizing caps, and the IEX volume problem
6. `docs/stage-b.md` — the brief, the off-switch, disqualify rules, the journal
7. `docs/backtest.md` — the validation protocol; read before believing a number
8. `docs/paper-trading.md` — position tracking, sell rules, the $100/week test
9. `docs/launch-checklist.md` — the ordered path to a live run
10. `docs/morning-routine.md` — **what to actually do each morning**

## Quick start

```bash
cp .env.example .env && chmod 600 .env    # fill in credentials
pip install -r requirements.txt
python3 notify/send_test.py               # verify Telegram
python3 notify/test_telegram.py           # 22 unit tests
python3 pipeline/test_llm.py              # 6 integration tests
./run_tests.sh                            # all 388 tests
python3 core/demo.py                      # screen on synthetic data
python3 -m pipeline.stage_a_nightly --offline           # nightly screen, no keys
python3 -m pipeline.stage_b_brief --offline --dry-run   # the brief, nothing sent
python3 -m backtest.run --offline --walk-forward        # honest validation
python3 -m pipeline.weekly_review --offline --dry-run   # the Sunday review
python3 -m paper.run --offline                          # one-month paper test
python3 -m paper.run --offline --scan                   # every month, for context
python3 -m pipeline.positions --offline                 # what do I hold, what to sell
```

## Layout

```
architecture.md          the design doc
docs/                    setup walkthroughs
notify/                  Telegram delivery (built, tested)
pipeline/                llm.py built; stage_a/stage_b are the next work
core/                    indicators, screen, ranking, sizing, regime, exits
paper/                   paper account, broker, one-month test driver
mcp/market_data/         cache, providers, universe — built
mcp/news/                news, filings, mechanical disqualify rules — built
mcp/journal/             append-only brief + fill journal — built
pipeline/agents/         catalyst, thesis, editor + JSON harness — built
pipeline/                stage_a, stage_b, watchdog, fill_listener,
                         weekly_review — built
backtest/                panels, engine, walk-forward, metrics — built
CLAUDE.md                persistent project context
.claude/skills/          6 skills: screen, sizing, data, backtest, brief, cost
vendor/tokenwise/        cost layer
deploy/                  bootstrap, systemd units, deploy script
config/screen.yaml       every threshold, versioned
```

## Status

**Feature-complete for v1 and entirely runnable offline.** Stage A screens and
sizes, Stage B briefs and journals, the fill listener logs replies, the weekly
review joins them, and the backtest harness validates the whole thing
walk-forward. 324 tests passing.

Position tracking and the $100/week paper test are in: reply `TOOK AAPL 100 @
182.50` to a brief and the system tracks the position and tells you when to
sell. `docs/paper-trading.md` has the details.

Still missing: chart images in the brief, and a point-in-time universe for
honest backtests.

**Nothing here has touched real market data.** `docs/stage-a.md` explains the
IEX volume issue that first contact will surface, `docs/stage-b.md` the
realized-vol stand-in for VIX, and `docs/backtest.md` the survivorship problem.

*Personal tool. Not investment advice.*
