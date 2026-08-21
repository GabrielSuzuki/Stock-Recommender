# Reading the brief — a beginner's decoder

Using the real brief from 2026-08-20, line by line.

---

## The header

```
Morning Brief — Thu 20 Aug
🟢 RISK_ON — Constructive tape with SPY above both moving averages
             and 75% breadth; risk-on setup supports taking new longs on dips.
```

**"The tape"** just means the overall market's condition.

**SPY** is an ETF that tracks the S&P 500 — a stand-in for "the market."

**"Above both moving averages"** — a moving average is the average closing
price over the last N days, redrawn daily. The 50-day and 200-day are the two
everyone watches. Price above both means the market has been rising over both
the medium and long run.

**"75% breadth"** means 75% of the 500 companies are above their own 200-day
average. This matters more than the index level. An index can be dragged
upward by five giant companies while the other 495 quietly fall — breadth tells
you whether the rise is broad or narrow.

**The traffic light is the most important thing in the brief:**

| | Meaning |
|---|---|
| 🟢 RISK_ON | Market conditions are healthy. Normal position sizes. |
| 🟡 NEUTRAL | Mixed. Half size, three positions maximum, be fussy. |
| 🔴 RISK_OFF | **Buy nothing new today.** Manage what you own and walk away. |

This verdict is computed by fixed rules, not written by the AI. That's
deliberate: a model asked "does the market look OK?" every morning will
eventually say yes on a day the rules say no, and say it convincingly. The
off-switch isn't something you want to be talked out of.

Historically, most of the damage in this style of trading comes from taking
perfectly good setups in a bad market. Roughly half of all four-week periods in
testing were RISK_OFF for the entire month.

---

## A single pick, decoded

```
PSX  ★★★★
entry 242.19  ·  stop 229.50  ·  target 273.94
14.0 sh  ·  risk $178
```

### The three numbers that matter

**Entry $242.19** — roughly where you'd buy. It's yesterday's close, so today's
actual price will differ. Not a limit order, just the reference point.

**Stop $229.50** — the price at which you sell and admit you were wrong. This
is the single most important number in the brief. Decided *before* you buy,
when you have no money on the line and can think clearly.

**Target $273.94** — where you'd take profit.

### "Risk $178" — what you're actually betting

You buy 14 shares at $242.19 = **$3,390 of stock**. But the $3,390 isn't what
you're risking. If it drops to your $229.50 stop, you sell and lose:

```
14 shares × ($242.19 − $229.50) = $178
```

**$178 is the bet. $3,390 is just what's parked in the trade.**

That's the mental shift that separates people who survive from people who
don't. The position size looks big; the risk is small and predetermined.

$178 is 0.7% of a $25,000 account. You could be wrong fourteen times in a row
and still have 90% of your money.

### "R" — the unit everything is measured in

**1R = the amount you risk on a trade.** Here, 1R = $178.

The target is at $273.94, which is $31.75 above entry — 2.5× the $12.69 you're
risking per share. So the target is **+2.5R**.

This is why R exists as a unit: it makes trades comparable. A $178 loss on PSX
and a $186 loss on ABNB are both **−1R**. You don't need to think in dollars,
just in "how many units of my risk did that make or lose."

The practical consequence: **you can be wrong more often than right and still
make money.** Win 40% of the time at +2.5R and lose 60% at −1R, and you come out
ahead. The system is built around that arithmetic, not around being right.

### The stars

Conviction, 1–5, written by the AI. PSX got 4; most get 3.

**Position size tells you nothing about conviction.** It's the output of
mechanical caps — the smallest position in the brief is usually just the one
that happened to be sized last. Only the stars mean confidence.

---

## The thesis and the invalidation

```
RS rank 94.1 with a 5.9% rising 200-day slope, sitting 0.53% off the 52-week
high, and the tightest volatility profile in the energy pair at 2.62% ATR.
```

**RS rank 94.1** — relative strength. Its price performance over the past year,
ranked against the other 500 companies, on a 1–99 scale. 94 means it
outperformed about 94% of them. The screen only accepts names above 70.

**"5.9% rising 200-day slope"** — the long-term average is 5.9% higher than it
was a month ago. The trend is not just up, it's steepening.

**"0.53% off the 52-week high"** — trading within half a percent of its best
price in a year. Counterintuitively, this is what you *want*. The strategy buys
strength, not bargains.

**"2.62% ATR"** — Average True Range: how much it typically moves in a day, here
about 2.6%. This is why stops differ between stocks. A quiet stock gets a tight
stop; a jumpy one gets a wide stop and *fewer shares*, so the dollar risk stays
the same. MRNA in an earlier brief had a 6.5% ATR — two and a half times as
jumpy — so it got a much wider stop and a much smaller position.

```
invalid if  A sharp crude/crack-spread break that drags the whole refining
            group lower — if PSX loses the 52-week-high shelf and the 200-day
            slope flattens, the trend premise is gone regardless of the stop.
```

**Read this line before the thesis.** The case *for* a trade always sounds
convincing. The specific condition that would prove it wrong is the more
useful sentence, and it's the one you'll want three weeks from now when you're
staring at a position and wondering whether to hold.

Note "regardless of the stop" — sometimes the reason to exit arrives before the
price does.

```
note: Kept over APA — same sector, same macro driver. PSX ranks higher…
```

The `note` explains what got **dropped** and why. Often the most informative
line in the brief. Here: PSX and APA are both energy — buying both isn't
diversification, it's the same bet twice.

---

## The footer

```
heat 4.0% · 2 disqualified
```

**Heat 4.0%** — add up the risk on every open position and it's 4% of your
account. That's the ceiling. If everything went wrong at once you'd lose 4%.
At 4.0% you're full; nothing new gets added until something closes.

**2 disqualified** — two stocks passed the technical screen and were then
vetoed by news: earnings due inside the holding window, a share offering, or
similar. These are mechanical rules, not judgment calls. A chart can look
perfect and be uninvestable for reasons only the filings know.

---

## What this system is and isn't

**It is not a prediction.** Nothing here knows what a stock will do. It finds
companies in established uptrends, checks nothing disqualifying is scheduled,
and computes an exit before you enter.

**It is a process.** The value is that the same rules run every day, position
sizes are calculated rather than felt, every trade has a predetermined exit,
and everything is logged for review. That's a real advantage over trading by
gut — but it's a different advantage from "this makes money."

**Swing trading loses money for most people who attempt it.** A well-built
pipeline changes the quality of your decisions. It does not change the odds of
the underlying activity, and no amount of engineering will.

**Nothing here has been validated against real outcomes yet.** The backtests
run on synthetic data. The first live screen was days ago. Which is exactly why
the plan is paper money for a month first.

---

## Five things to hold onto

1. **The stop is the trade.** Everything else is commentary.
2. **Think in R, not dollars.** It makes losses survivable and trades comparable.
3. **🔴 means stop.** A day with no trades is the system working.
4. **Skipping every pick is a legitimate morning.** So is taking one.
5. **If you don't understand why a name is there, don't buy it.** "The computer
   said so" is not a reason, and this is your money.

---

*Not investment advice. This is a personal tool built by and for one person who
is still learning, and it is unproven.*
