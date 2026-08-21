# When to trade real money

The honest answer, worked out rather than guessed.

---

## The uncomfortable arithmetic

This system takes roughly **6 round trips a month** (6 positions, 1–4 week
holds). Per-trade results vary by about ±1.5R. Given that, here is how long it
takes before a measured result means anything:

| Period | Trades | 95% confidence interval on measured edge | Verdict |
|---|---|---|---|
| 1 month | 6 | −0.72R to +1.62R | tells you nothing |
| 2 months | 13 | −0.38R to +1.28R | tells you nothing |
| 3 months | 19 | −0.23R to +1.13R | tells you nothing |
| 6 months | 38 | −0.03R to +0.93R | tells you nothing |
| **12 months** | **76** | **+0.11R to +0.79R** | first real signal |

To *prove* a +0.45R edge is real at 80% power takes **87 trades — about 14
months**. If the true edge is a more modest +0.20R, it takes **70 months**.

**So: you cannot wait for proof that this works. Proof is years away, and by
then the market regime that produced the edge may have changed anyway.**

Anyone who tells you three good months validates a strategy is misreading noise.
So is anyone who abandons one after three bad months.

---

## What follows from that

Since performance can't be the gate, two things must be true instead:

1. **Gate on process**, which *is* measurable in weeks.
2. **Start at a size where being wrong doesn't matter**, because you will be
   going live without proof and should size accordingly.

---

## Minimum wait: 2 months paper, not 1

One month is ~6 trades. That is not enough to see the exits work, let alone
anything else. Two months gets you ~13 round trips — still statistically
meaningless for edge, but enough to observe the machinery under varied
conditions and, more importantly, enough to observe **yourself**.

Extend to three if the first two months were entirely RISK_ON. You want to have
sat through at least one stand-down stretch before real money is involved.

---

## Required before going live — all of them

### Process (non-negotiable)

- [ ] **20+ consecutive weekdays with no missed run.** A system that skips
      mornings isn't a system.
- [ ] **You opened and read the brief at least 80% of mornings.** If it became
      an unread notification by week three, that is the finding. Fix the format
      or stop.
- [ ] **You logged fills honestly**, including the days you took nothing.
- [ ] **Recommended-vs-taken above 30%.** Below that you aren't running this
      system, you're overriding it, and the results won't be its results.
- [ ] **You accepted every 🔴 RISK_OFF day** without looking for a way around it.
- [ ] **Every exit inspected against a chart** and judged reasonable — especially
      any `gap_stop`.

### Understanding

- [ ] You can explain, without looking it up, what R is and why position size
      isn't the bet.
- [ ] You've read one weekly review and understood the recommended-vs-taken gap.
- [ ] **You have written down what will make you turn this off.** Decide now,
      while nothing is at stake.

### Results (a floor, not a target)

- [ ] **Expectancy better than −0.67R.** Over ~19 trades, if the true edge were
      exactly zero, 95% of outcomes land between −0.67R and +0.67R. A result
      *worse* than −0.67R is unlikely to be luck — that is a genuine abandon
      signal. Anything inside that band is noise, **including a good result.**
- [ ] **No exit you can't explain.** One inexplicable fill is worth more
      attention than a whole month of P&L.

Notice what is *not* on this list: profitability. A profitable paper month is
not evidence, and requiring one would just select for a lucky sample.

---

## What to start with

Whatever amount you would be genuinely fine losing entirely. Not "would prefer
not to lose" — **fine.**

The mechanics: 6 positions × 0.75% risk = **4% total portfolio heat**. If a
sector unwind stops all six together, that's −4% in days. The backtest's worst
drawdown was **−10.2%, and it spent 731 days underwater**.

| Account | A −4% heat day | A −10% drawdown |
|---|---|---|
| $500 | −$20 | −$50 |
| $1,000 | −$40 | −$100 |
| $2,500 | −$100 | −$250 |
| $5,000 | −$200 | −$500 |

At $500–1,000 the dollar amounts are small enough that you'll behave normally,
which is the entire point of the first live stretch: you're still testing your
own behaviour, just with skin in the game. Fractional shares make small accounts
workable.

**Scale up only after 6 months live**, and only if the process checklist is
still being met. Not because the returns were good — you still won't know that.

---

## The comparison that actually matters

Every backtest report prints buy-and-hold SPY over the same period, on purpose.

If after a year this system has underperformed simply owning an index fund —
with more risk, more decisions and 10 minutes of your morning — that is a real
and useful finding, and the correct response is to stop.

Track it from day one. It's the only benchmark that means anything.

---

## Turn it off if

Write your own list, but these are the defaults:

- Expectancy below −0.67R over 20+ trades
- Drawdown worse than −15%
- You override the system on more than a third of picks
- You stop reading the brief
- You find yourself changing thresholds after losing weeks
- It underperforms SPY over 12 months

**That last one is the one people ignore.** Beating the market is hard, most
professionals fail at it, and there is no shame in concluding an index fund is
the better answer for you. The engineering here was still worth doing — you now
understand markets, risk and your own behaviour far better than you did.

---

## The one-line version

> Paper trade for **two months** minimum. Go live only if the **process**
> checklist passes — not because the results were good, since two months of
> results cannot be good or bad in any meaningful sense. Start with money you
> would be fine losing entirely. Expect to need **a year** before performance
> data means anything at all.

---

*Not investment advice. These thresholds are one reasonable framework, not the
only one, and reasonable people would set them differently.*
