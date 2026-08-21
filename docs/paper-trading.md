# Position tracking and the paper test

Two things: a tracker that watches what you own and tells you when to sell, and
a one-month simulation that runs the whole system on virtual money.

---

## 1. Telling the system you bought something

Reply to the morning brief:

```
TOOK AAPL 100 @ 182.50
```

That's it. `BOUGHT`, `SOLD`, `STOPPED`, `CLOSED`, `at` instead of `@`, `$`, and
thousands separators all work. Anything that doesn't parse gets a short "didn't
understand" reply rather than a guess — a mis-parsed fill silently corrupts
every future review.

Positions are derived from those fills by FIFO netting, so nothing has to be
kept in sync by hand.

**Off-plan buys are tracked too.** If you buy something the system never
recommended, it still appears — with a stop derived from its ATR (2×), or 8%
below cost if no ATR is available. An untracked position is the one that hurts
you; refusing to track it would be the worst possible response to slightly
irregular input.

---

## 2. When to sell

`core/exits.py`. Deterministic — the model never decides to sell. Rules are
evaluated in priority order and the first that fires wins.

| | Rule | Action |
|---|---|---|
| 1 | price traded to or through the stop | **SELL** |
| 2 | first target reached | **TRIM** 50%, stop to breakeven |
| 3 | held ≥ 40 sessions | **SELL** |
| 4 | closed below the 50-day | WATCH |
| 5 | RS rank below 50 | WATCH |

**The stop is checked against the day's LOW, not the close.** A position that
traded through its stop is out, whatever it closed at. Checking the close would
report "still holding" on a name your broker already sold.

**Stops only ratchet up.** Two ladders:

- at **+1R**, the stop moves to breakeven — a trade that reached 1R and then
  became a loser is the single most avoidable bad outcome
- at **+2R**, it trails 2.5 ATRs behind, floored at (R−1) locked in — wider than
  the entry stop on purpose, because tightening a trailing stop to entry-stop
  width is how people get shaken out of the trades that pay for the year

`update_stop` returns `max(current, computed)`. A rule that could *lower* a stop
would silently widen risk on exactly the positions that had started working.

**R is anchored to the initial stop, always.** If R were recomputed against a
trailing stop, every ratchet would inflate the reported R-multiple and a
mediocre trade would look like a good one.

### Where you see it

- **In the morning brief**, under `MANAGE FIRST`, above the regime consequences
  and above any new ideas. Managing what you own outbids finding something new,
  and on a RISK_OFF morning it is the only thing that matters.
- **`python3 -m pipeline.positions`** — full book on demand.
- **10:00 and 12:45 Pacific** via `screener-positions.timer`, to catch intraday
  stop and target hits the 05:00 brief could not have seen.

---

## 3. The one-month paper test

```bash
python3 -m paper.run --offline                    # simulate a month instantly
python3 -m paper.run --offline --scan             # every month in the history
python3 -m paper.run --start 2026-05-20           # a specific month
```

$100 deposited every Monday for four weeks, unused cash carrying over. The real
screen, the real sizing, the real exit rules, against a simulated account.

### Two decisions I made for you

**Fractional shares are on.** At $400 equity and 0.75% risk, the whole-share
model returns zero shares on any stock above about $20 — the account would sit
in cash for a month and prove nothing. Every major retail broker supports
fractional shares now, so this is realistic rather than a fudge. It does mean
the *dollar* results at this size are trivial; the *percentage* results are what
transfers.

**Cash is never negative.** An order that can't be paid for is rejected and
counted, not filled on margin. The rejection count is itself a finding: it tells
you the account was too small for the position sizes the screen wanted.

### A real run

```
PAPER TEST — $100/week for 4 weeks
  period              2026-05-20 -> 2026-06-17
  deposits            4 x $100 = $400.00
  ending equity       $414.31
  profit / loss       $+14.31  (+3.58% on deposits)
  round trips closed  4
  win rate            100% (4/4)
  average R           +2.35R
  exit reasons        {'target_partial': 3, 'stop': 1}

  SYN017   2026-05-26 -> 2026-06-03   792.19 ->   881.91   +2.46R  target_partial
  SYN017   2026-05-26 -> 2026-06-12   792.19 ->   860.48   +1.87R  stop
```

Look at SYN017 twice. It scaled out half at +2.46R, the stop ratcheted to
breakeven, and the remainder later stopped out at **+1.87R** — a profit, not a
loss. That's the breakeven ladder doing its job, and it is the clearest evidence
the exit machinery works.

Profit is measured **against deposits**, not as a raw equity curve. A curve that
rises because you kept paying in is not a return.

### What one month does and does not prove

A month is ~21 sessions and perhaps 5–15 round trips. The confidence interval on
a win rate from 10 trades runs from "excellent" to "terrible". **Treat the P&L
as noise.**

`--scan` makes that concrete by running every four-week window in the history:

```
  windows                34
  windows that traded    17
  best / worst month     $+14.31 / $-3.06
  median month           $+0.00
  profitable months      12/34
```

Half the windows never traded at all — the regime filter stood the account down
for the entire month. That spread is the point. Any single month is a draw, not
a measurement.

What a month **does** test well, and what makes it worth running:

- does the machinery run unattended, end to end, every day
- does the account get stuck — all cash, nothing affordable
- do the exits fire when they should, at sane prices
- is the brief something you'd actually read at 5 AM
- how often does the regime filter stand you down

Those are process questions, and a month answers them.

### Forward mode

`--offline` and `--start` replay history. For a genuinely out-of-sample test,
run against the live bar cache after Stage A has been populating it — same
command without `--offline`. The account persists to `data/paper/account.json`
between runs.

**My recommendation: run the forward test for the full month before funding
anything.** The simulated months tell you the code works. Only the forward one
tells you whether *you* will read the brief at 5 AM and act on it, which is the
component with the least evidence behind it.

---

## 4. What would make me more confident

In rough order of value:

1. **A forward month on real data.** Everything above is synthetic.
2. **Fills logged honestly**, including the ones where you ignored the brief.
   The weekly review measures the gap between recommended and taken, and that
   gap is usually the most actionable number in the system.
3. **Resisting the urge to change thresholds after a bad week.** The walk-forward
   already showed a +0.550R overfitting gap on synthetic data. Tuning on one
   month of live results would be worse.
