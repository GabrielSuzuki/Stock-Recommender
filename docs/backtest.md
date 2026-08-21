# Backtesting

```bash
python3 -m backtest.run --offline                 # smoke test on a synthetic market
python3 -m backtest.run --walk-forward            # the only version that is evidence
python3 -m backtest.run --no-regime               # measure what the filter costs
python3 -m backtest.run --json out.json --trades-csv trades.csv
```

**A single-split run is a smoke test, not a result.** `backtest/run.py` prints
that warning on every one, on purpose.

---

## Architecture

```
bars ──▶ build_panels() ──▶ run_backtest() ──▶ performance()
         date × symbol       event loop         metrics + the null
         matrices, once      per trading day
```

A naive backtest calls `compute_symbol_metrics` for every (symbol, date) pair —
500 × 750 = 375,000 full-history recomputations. Panels compute each symbol's
rolling series once and slice a row per date.

The critical property: **panels are built by calling the same functions
production calls**, not by reimplementing them in vectorized form. A backtest
that reimplements the screen is testing the reimplementation.
`test_panels_match_production_metrics` asserts panel rows equal
`compute_symbol_metrics` on random symbol/date pairs to nine decimal places. If
that test fails, nothing else in `backtest/` means anything.

---

## Timing, and where backtests cheat

```
signals   from the CLOSE of day D
orders    fill at the OPEN of day D+1
exits     checked from D+1 onward
```

Nothing is decided using a price that had not printed yet.

**Stops and targets are rescaled to the actual fill.** They are computed from
the D−1 close, so when the stock gaps overnight the intended R would otherwise
drift. The distance is preserved, not the price.

**Intrabar order is unknowable.** When a bar's range contains both stop and
target, the **stop is assumed to have hit**. Pessimistic by construction — an
optimistic assumption inflates the win rate on exactly the volatile bars where
it matters most.

**Gaps through the stop fill at the open**, not the stop price. Modelling every
stop as filling exactly at the stop is the commonest way a backtest understates
drawdown. Exit reasons distinguish `stop` from `gap_stop` so you can see how
often it happened.

---

## Walk-forward

```
|<-- train 2y -->|<- test 6m ->|
          |<-- train 2y -->|<- test 6m ->|
                    |<-- train 2y -->|<- test 6m ->|
```

Each training window selects parameters by a stated objective (`expectancy_r` by
default); those parameters are applied untouched to the following out-of-sample
window. **Only the concatenated out-of-sample segments are reported.**

Segments are chained **by return, not by level** — each starts fresh at the
starting equity inside its own window, so naive concatenation would reset the
curve every six months and make the reported CAGR meaningless.

Two guards worth knowing:

- **`min_trades=10`.** A parameter set that produced three trades has not been
  measured, it has been sampled. Selecting on tiny samples is the cheapest way
  to overfit.
- **`overfitting_gap`** — mean in-sample expectancy minus mean out-of-sample. It
  is a measurement of your own overfitting. Above ~0.3R, the search is fitting
  noise.

The default grid is deliberately small (two axes, three values each). A wide
grid over a short history is a machine for manufacturing false confidence.

---

## What is always reported

`performance()` prints, unconditionally:

- CAGR, total return, **max drawdown**, **longest time underwater**
- Sharpe and Sortino
- **Exposure** — percent of days with a position open. A strategy in the market
  8% of the time returning 6% a year is a very different proposition from one
  fully invested for the same return, and the raw number cannot tell them apart.
- Trades, win rate, **expectancy in R**, profit factor, average hold
- Total costs paid, and the exit-reason breakdown
- Regime day counts
- **The null**: buy-and-hold SPY over the same window, plus the excess

The null is not optional. If the strategy does not clear buy-and-hold after
costs and your time, that is a real and useful finding, and it is only visible
if you always measure it.

---

## Costs

`slippage_pct` defaults to **0.05% per side**, charged on both entry and exit,
plus optional per-trade commission. A frictionless backtest of a screen like
this looks fine and means nothing.

There is a test asserting that zero slippage beats realistic slippage — if costs
make no difference to the ending equity, they are not being applied.

---

## The offline fixture, and why it had to change

`core.synthetic.market()` generates a **correlated** universe: one market return
series with alternating bull and bear phases, plus per-symbol beta and
idiosyncratic noise.

The first version used independent stocks — one third uptrends, one third
downtrends, one third flat. The backtest returned **zero trades**: breadth sat
at ~33% every single day, below the 40% RISK_OFF threshold, so the regime filter
correctly refused to trade for six straight years.

That is the fixture being wrong, not the filter. A bag of independent series is
not a market. Breadth never moves together, so the regime filter either never
fires or fires constantly, and the trading path is never exercised. The
single-factor model produces the two properties the backtest actually needs:

- **breadth that swings**, so RISK_ON, NEUTRAL and RISK_OFF all occur
- **correlation**, so the sector cap has something to bite on and drawdowns
  cluster the way real ones do

---

## Survivorship

`load_universe()` returns **today's** S&P 500 membership. Correct for live
screening. Wrong for backtests: you would be running a 2019 screen over the
companies successful enough to still be in the index in 2026, having silently
deleted everyone dropped for underperforming.

Published estimates put the resulting overstatement at a few percentage points
of annual return — comfortably larger than any edge this screen could plausibly
find.

Either source a point-in-time membership series, or write the caveat into the
result and say by roughly how much. Do not quietly do neither.

---

## A bug worth remembering

The engine originally computed the regime by reproducing the thresholds
locally, passing an empty universe to `assess()` to avoid recomputing 500
rolling means per simulated day. That made breadth NaN, which degraded the
reading, which floored the verdict at NEUTRAL — **so RISK_ON was unreachable and
every backtest silently ran at half risk.** Nothing looked wrong; the reports
were plausible.

The fix was to give `assess()` an optional precomputed `breadth` argument so the
engine supplies the number but `core/regime.py` still owns every threshold. The
lesson generalises: when you duplicate a rule for performance, duplicate the
input, never the decision.

---

## Before you act on any of this

Paper trade for 60 days. The journal from those days is worth more than any
backtest here, because it is the only test that includes you as a component.
