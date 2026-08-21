# Launch checklist

Everything in this repo runs. Nothing in it has seen a real price. This is the
order to fix that in, and the honest time cost of each step.

Run `python3 -m pipeline.preflight` at any point — it checks all of this and
tells you what is blocking.

---

## Day 1 — get real data (about an hour)

**1. Alpaca keys — 5 minutes.** Free paper account at alpaca.markets. You need
`ALPACA_API_KEY_ID` and `ALPACA_API_SECRET_KEY`. No funding, no approval wait.
Everything else on this list is optional; this one is not.

**2. Telegram bot — 10 minutes.** `docs/setup-telegram.md`. Without it the
brief has nowhere to go.

**3. `pip install -r requirements.txt`** and fill in `.env` from `.env.example`.

**4. Run preflight.**

```bash
python3 -m pipeline.preflight
```

Fix anything marked `✗`. Warnings are fine to start — the system degrades
rather than failing.

**5. Deal with the volume scale.** *(Measured on 2026-08-21: the feed reports
3.5% of consolidated tape and `min_avg_volume` is already calibrated to 14,000
feed units. Re-run preflight if you change data provider.)* This is the step that matters most and the
one you will be tempted to skip.

Preflight measures Alpaca's IEX-only volume against consolidated reference
figures for five very liquid names. If the feed reports only a few percent of
the real tape — which is what IEX-only means — then `min_avg_volume: 400000`
will reject essentially the entire S&P 500, the screen will return an empty
list every single morning, and it will look exactly like a slow market.

```bash
python3 -m pipeline.preflight --fix-volume-scale
```

That writes the measured multiplier into `config/screen.yaml`, and
`resolve_provider` now reads it. **Sanity-check the number before trusting it**
— the reference ADVs in `preflight.py` are order-of-magnitude, not live.

---

### If the universe check fails

```
python -m mcp.market_data.universe
```

That runs the fetch on its own and says exactly which step broke — the HTML
parser, the network, or the table shape. `load_universe` deliberately swallows
these into one message (it must not crash the 01:00 job over a Wikipedia
hiccup), so this diagnostic exists to un-swallow it.

Two sources are tried: the rendered Wikipedia page, then the MediaWiki API.
Both send a descriptive User-Agent — Wikipedia rejects urllib's default with a
bare **HTTP 403**, which looks like a firewall problem and is not one.

**If both are blocked**, supply the list by hand — it only has to happen once:

1. Copy the S&P 500 constituents from any source into a CSV with a `symbol`
   column (a `sector` column is used if present, and improves the
   concentration cap)
2. `python -m mcp.market_data --from-csv path\to\list.csv`

Ticker format doesn't matter — `BRK-B`, `BRK.B` and `BRK/B` all normalise.

If you're on Windows and see a path like `\opt\screener\...`, that's a
leftover folder from an older version — delete `C:\opt\screener` and re-run.

---

## Day 1 — first live screen (30 minutes)

```bash
python3 -m pipeline.stage_a_nightly
```

Then open `data/candidates.json` and ask one question: **do these look like
stocks you would consider?**

If the list is empty, read the `funnel` block. `c1_above_ma50` killing
everything means something upstream is broken. `c8_rs_rank` doing the killing
means the market genuinely has no leadership — that is the screen working.

If the liquidity gate is eliminating almost the whole universe, go back to the
volume scale.

Then:

```bash
python3 -m pipeline.stage_b_brief --dry-run
```

Read the brief you get. Would you act on it at 5am? That question has no
technical answer and it is the most important one here.

Then check what it cost:

```
python -m pipeline.cost
```

Expect roughly **$0.20–0.30** for a full Stage B. If it is wildly more,
something is looping — stop and investigate before automating it.

**Do this by hand for three or four mornings before automating anything.**

---

## Week 1 — deploy (about 40 minutes)

`docs/setup-vps.md`. Hetzner CX22, ~$5/mo. The bootstrap script is idempotent.

The one thing to verify after install:

```bash
systemctl list-timers 'screener-*' --all
```

The `NEXT` column must show **Pacific** times. If it shows UTC, the timezone
step did not take, and your 5am brief will arrive at 9pm.

Then test the failure path, which is the step everyone skips:

```bash
mv /opt/screener/data/candidates.json /tmp/
systemctl start screener-brief.service
```

You should get a Telegram alert, not silence and not a stale brief.

---

## Weeks 1–5 — the paper month

Let it run. Every morning: read the brief, and reply to it with what you
actually did.

```
TOOK AAPL 100 @ 182.50
```

**Log the trades you skipped, too** — or rather, don't log them, and let the
weekly review show you the gap. `names_recommended` versus `names_acted_on` is
usually the most actionable number in the whole system, and it only exists if
you are honest about the fills.

In parallel, run the simulated paper account so the two can be compared:

```bash
python3 -m paper.run          # against the live cache, forward
```

Sunday evenings you get the review automatically.

### What to look for, in priority order

1. **Did it run every day?** Missed mornings are the failure that matters.
2. **Did the exits fire at sane prices?** Check any `stop` or `gap_stop` exit
   against the actual chart.
3. **Did the account get stuck** — all cash, nothing affordable?
4. **How often did the regime filter stand you down?** On the synthetic data,
   half of all four-week windows never traded at all.
5. **Did you read the brief?** If you stopped opening it by week two, that is a
   finding about the design, not about you.

### What NOT to look for

**The P&L.** A month is ~21 sessions and maybe 5–15 round trips. The confidence
interval on a win rate from ten trades runs from excellent to terrible. Running
every four-week window over the synthetic history gave a best month of +$14.31
and a worst of −$3.06 on $400 deposited, with a median of exactly zero. Any
single month is a draw.

---

## The trap

After a bad week you will want to loosen a threshold. Don't.

The walk-forward already measured a **+0.550R overfitting gap** on synthetic
data with no real edge in it — the parameter search fitting noise, and flagging
itself. Tuning on one month of live results would be strictly worse than that.

If you want to change something, change it in `config/screen.yaml`, commit the
diff, and re-run the walk-forward before and after. A change justified by a
single good or bad month is a change justified by noise.

---

## Before real money

- [ ] 20+ consecutive trading days with no missed run
- [ ] Every exit inspected against a chart and judged reasonable
- [ ] The volume scale verified against a source you trust, not just preflight
- [ ] At least one weekly review read, with the recommended-vs-taken gap
      understood
- [ ] A written answer to: *what will make me turn this off?*

That last one is the real gate. Decide it now, while nothing is at stake.

---

## What is still missing

| | Why it matters |
|---|---|
| Chart images in the brief | Fastest way to catch the model being wrong |
| Point-in-time universe | Every backtest number is survivorship-inflated until this exists |
| Live-data calibration | The IEX volume scale is measured, not yet verified |
| Fundamentals overlay | Deferred to v2 on purpose; revisit only with journal evidence |
