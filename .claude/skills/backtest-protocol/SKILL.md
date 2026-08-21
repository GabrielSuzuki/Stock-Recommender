---
name: backtest-protocol
description: The rules a backtest must follow before its output counts as evidence. Use when running or interpreting a backtest, changing backtest/, or evaluating a proposed screen change.
---

# Backtest protocol

Exists so the backtest cannot grade its own homework.

## The five rules

**1. Walk-forward, never a single split.** Optimize on a rolling 2-year window,
test on the following 6 months, roll forward. `--walk-forward`. A single-split
run is a smoke test; `backtest/run.py` prints that warning on every one.

**2. Report out-of-sample only.** In-sample numbers are computed and kept, but
they are diagnostic. The `overfitting_gap` (in-sample minus out-of-sample
expectancy) is the useful thing about them — above ~0.3R means the parameter
search is fitting noise.

**3. Realistic costs.** `slippage_pct` defaults to 0.05% per side, charged on
both. A frictionless backtest of a screen like this looks fine and means
nothing. There is a test asserting that zero slippage beats realistic slippage —
if costs make no difference, they are not being applied.

**4. Fix survivorship or state it.** `load_universe()` returns today's S&P 500
membership. Backtesting with it screens 2019 over the companies successful
enough to still be in the index in 2026. Published estimates put the
overstatement at a few percentage points of annual return — larger than any edge
this screen plausibly has. Either source point-in-time membership or write the
caveat into the result.

**5. Always report the null.** Buy-and-hold SPY over the same window, every
time. If the strategy does not clear it after costs and your time, that is a
real and useful finding, and it is only visible if you always measure it.
`performance()` includes it unconditionally.

## Timing model

```
signals from the CLOSE of day D
orders fill at the OPEN of day D+1
exits checked from D+1 onward
```

Nothing is decided using a price that had not printed. The one approximation is
intrabar: when a bar's range contains both stop and target, **the stop is
assumed to have hit**. Pessimistic by construction — being optimistic there
inflates the win rate on exactly the volatile bars where it matters.

Gaps through the stop fill at the **open**, not the stop price. Modelling every
stop as filling exactly at the stop is the commonest way a backtest understates
drawdown.

## The test that keeps it honest

`test_panels_match_production_metrics` asserts the vectorized panels equal
`compute_symbol_metrics` exactly, on random symbol/date pairs. A backtest that
reimplements the screen is testing the reimplementation. If this fails, nothing
else in `backtest/` means anything.

## Pre-registration

Before adding any indicator or condition: state the hypothesis, run it
out-of-sample, accept the result. For candlestick patterns specifically, the
published evidence says expect no edge — if the backtest disagrees, suspect the
backtest first.

## Offline fixture

`core.synthetic.market()` builds a **correlated** universe with bull/bear
phases, not independent stocks. This matters more than it sounds: with
independent series, breadth never moves together, so the regime filter either
never fires or fires constantly and the trading path is never exercised. That is
exactly what happened on the first run — zero trades, 84% RISK_OFF days.

## Before acting on a number

Paper trade for 60 days. The journal from those days is worth more than any
backtest, because it is the only test that includes you as a component.
