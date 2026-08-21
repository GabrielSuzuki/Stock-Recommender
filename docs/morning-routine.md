# The morning routine

Ten minutes, 5:00–6:30 AM Pacific. Market opens 6:30.

While you're still running by hand, this is the whole job.

---

## The three commands

```bash
cd path\to\stock-recommender

python -m pipeline.stage_a_nightly        # ~30s warm. Screen + size.
python -m pipeline.stage_b_brief          # ~50s. Regime, news, theses, Telegram.
python -m pipeline.positions              # what you already own, and what to sell
```

Drop `--dry-run` off Stage B once you want it on your phone. Add it back any
time you want to look without sending.

**Never run `preflight` between them** — it's a setup tool, not a daily one.

---

## Reading the brief

It arrives in this order on purpose. Work down it.

### 1. `MANAGE FIRST` — if present

Sell and trim signals for what you already hold. **This outranks everything
else.** Managing open risk beats finding new ideas, and on a 🔴 morning it's
the only section that matters.

| | Meaning |
|---|---|
| 🔴 **SELL** | Stop hit, target hit, or held past the window. Act at the open. |
| 🟠 **TRIM** | First target reached. Take half, stop moves to breakeven. |
| _watch:_ | Below the 50-day, or relative strength decaying. No action — just know. |

### 2. The regime line

| | What to do |
|---|---|
| 🟢 RISK_ON | Normal size, take the setups |
| 🟡 NEUTRAL | Half risk, three positions max, be fussy |
| 🔴 RISK_OFF | **No new entries.** Manage existing, close the app. |

A stand-down day is the system working. Roughly half of all four-week windows
in testing never traded at all.

### 3. The picks

For each: entry, stop, target, share count, thesis, invalidation.

**Read the `invalid if` line before the thesis.** It's the specific thing that
would mean the idea is wrong, and it's more useful than the argument for it.

**Ignore position size as a signal.** It's the output of four mechanical caps —
a small position usually means the heat budget ran out, not that the setup is
weak. Conviction is the ★ rating and nothing else.

Check `_note:_` — that's where it explains what it *dropped* and why. Often the
most informative line in the brief.

### 4. The footer

`heat 4.0%` — total open risk. At the 4% cap, you're full.
`2 disqualified` — names the screen liked that the news vetoed.
`DEGRADED RUN` — something fell back. The brief is thinner than usual; trust it less.

---

## Deciding

For each pick, three questions:

1. **Do I understand why it's here?** If the thesis doesn't make sense to you,
   skip it. "The computer said so" is not a reason.
2. **Can I live with the stop?** Look at the distance. A 13% stop on a 6% ATR
   biotech is correct sizing and still might be more than you want to watch.
3. **Am I already in this trade?** Three energy names is one bet. The system
   catches the obvious cases; it can't catch that you already own a refiner in
   another account.

**Skipping every pick is a legitimate morning.** So is taking one.

---

## Logging — the part that matters

Reply to the Telegram brief:

```
TOOK PSX 14 @ 242.50
SOLD ABNB 16 @ 201.00
```

`BOUGHT`, `STOPPED`, `CLOSED` and `at` instead of `@` all work. You get a ✓ back.

**Log fills honestly, including the ones you skipped** — by not logging them.
The weekly review measures recommended-vs-taken, and that gap is usually the
most actionable number the whole system produces. A journal you curate is a
journal that can't teach you anything.

If you trade something the system never recommended, log it anyway. It gets
tracked with an ATR-derived stop, and shows up as an off-plan buy in the review.

---

## When something looks wrong

| Symptom | First thing to check |
|---|---|
| No brief by 5:15 | Did Stage A run? `data\candidates.json` timestamp |
| Empty candidate list | The `funnel` in `candidates.json`. If c8 did the killing, that's a real market read. If c1 killed everything, something's broken. |
| Tickers look fake (SYN…) | You ran preflight after Stage A. Re-run Stage A. |
| Brief says DEGRADED | Check which feed failed in the log. Usually Finnhub. |
| Cost spike | `python -m pipeline.cost`. Normal is ~$0.13/day. |

When in doubt: **don't trade the brief you don't trust.** Nothing here is
time-critical enough to justify acting on output you're unsure about.

---

## Weekly, Sunday evening

```bash
python -m pipeline.weekly_review --weeks 4
```

Look at, in order:

1. **Did it run every day?** Missed mornings are the failure that matters.
2. **`names_recommended` vs `names_acted_on`.** If you're taking 10%, either
   the picks are bad or the format isn't working. Both are fixable; neither
   fixes itself.
3. **Off-plan buys.** Not wrong, but invisible to every other measurement.
4. **Exit prices.** Pull up a chart on anything that stopped out. Did it fill
   where it should have?

**Do not look at the P&L for the first two months.** Ten trades tells you
nothing — the confidence interval runs from excellent to terrible.

---

## The trap

After a bad week you will want to loosen a threshold.

The walk-forward already measured a **+0.550R overfitting gap** on data with no
real edge in it — that's a parameter search fitting noise and flagging itself.
Tuning on one live month would be strictly worse.

If you want to change something: edit `config\screen.yaml`, commit the diff,
and re-run the walk-forward before and after. A change justified by one good or
bad month is a change justified by noise.

---

## Once this is a habit

Deploy to the VPS (`docs\setup-vps.md`, ~40 min) and the first two commands run
themselves at 01:00 and 05:00. The brief just arrives. Your morning becomes
reading it and replying with what you did — which is the only part that was
ever really yours.
