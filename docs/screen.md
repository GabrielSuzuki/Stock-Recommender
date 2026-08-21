# The Trend Template screen

`core/` is the deterministic layer: bars in, ranked candidates out, no LLM anywhere. Every number the morning brief cites originates here, which is what makes the brief reproducible and the API bill small.

```
bars_by_symbol ──▶ indicators.build_metrics_frame ──▶ screen.evaluate ──▶ ranking.top_candidates
                        (per-symbol metrics,            (2 gates +           (weighted percentiles,
                         then cross-sectional RS)        8 conditions)        capped at 20)
```

Run it: `python3 core/demo.py`. Test it: `python3 core/test_screen.py` (61 tests).

---

## The eight conditions

Strict AND, per locked decision #2. Thresholds live in `config/screen.yaml`; nothing is hard-coded.

| | Condition | Config key |
|---|---|---|
| gate | close ≥ $5 | `universe.min_price` |
| gate | 50-day avg volume ≥ 400k | `universe.min_avg_volume` |
| c1 | close > 50-day MA | — |
| c2 | close > 150-day MA | — |
| c3 | close > 200-day MA | — |
| c4 | 50-day > 150-day | — |
| c5 | 150-day > 200-day | — |
| c6 | 200-day rising for ≥ 21 bars | `trend_template.ma200_rising_days` |
| c7 | within 25% of 52-week high | `trend_template.max_pct_below_52wk_high` |
| c8 | RS rank > 70 | `trend_template.min_rs_rank` |

Gates are separated from the template so the funnel never reports "failed the trend template" when a stock was really just too illiquid to trade.

---

## Three findings from building it

### 1. Conditions 2 and 3 are logically redundant

c1 (close > MA50) and c4 (MA50 > MA150) together **imply** c2 (close > MA150). c2 and c5 (MA150 > MA200) together imply c3. There is no price configuration where c2 or c3 fails alone — the test suite proves this by trying.

So the eight conditions are really **six independent constraints plus two that can only fail when something else already has**. This shows up directly in the funnel: c2, c3, c4 and c5 eliminate zero names in the demo run, every time.

This is not a defect in the template — Minervini states them separately because it's clearer to a human reading a chart.

**Correction, 2026-08-21, from the first live run.** I originally wrote that the
funnel "will never credit c2 or c3 with a rejection". That is wrong, and real
data showed it immediately: on a 503-name S&P 500 screen, c2 eliminated 46 and
c3 eliminated 10.

The proof still holds. The funnel evaluates in Minervini's order, so **c2 is
tested before c4**. A name with `MA50 < close < MA150` passes c1 and fails c2 —
and necessarily also fails c4, which simply hasn't been reached yet. The set of
passing names is identical either way; only the attribution moves.

Synthetic data hid this for a simple reason: monotonic price series never
produce `close > MA50` while `MA50 < MA150`. Real recovering stocks do it
constantly.

The funnel now reports **two counts**: `eliminated` (order-dependent) and
`sole` — names *only* that condition rejects, which is what you would let
through by deleting it. **`sole` is always zero for c2 and c3.** That is the
column that reflects the redundancy.

It still matters that anyone building a "6 of 8" scored variant would be
**double-counting the moving-average stack** — three of eight slots measuring
one thing. That remains a concrete reason the strict-AND decision was right.

### 2. Condition 7 does independent work, and the case is specific

I initially couldn't construct a price path that fails c7 alone, and nearly concluded it was redundant too. That was wrong — a flaw in the search, described in finding 3.

The shape that isolates c7 is a **short isolated spike**: a one-to-five-day squeeze (buyout rumour, index add, short squeeze) that sets a 52-week high the stock never revisits. A one-day spike barely moves a 200-day average, so the entire MA structure stays intact and RS stays strong — but the stock now trades ~35% below its 52-week high.

That's the name c7 exists to reject, and the argument for rejecting it is good: its recent high was a liquidity event, not a level real buyers defended. Without c7 the screen buys it. `synthetic.spiked_then_faded()` generates it; `test_spike_high_rejected_on_c7_alone` asserts it fails c7 and nothing else.

Smooth drawdown-and-recovery shapes, by contrast, never fail c7 alone — I swept ~600 of them. A name 25%+ off its high after an ordinary decline has always broken its MA structure too. So c7's independent contribution is narrow but real, and specifically about spikes.

### 3. RS rank is cross-sectional, which makes the universe a screen parameter

`min_rs_rank: 70` doesn't admit "strong stocks." It admits **roughly the top 30% of whatever you feed it**. Two consequences:

- **A one-symbol universe always scores 50 and can never pass c8.** This is correct behaviour for a percentile rank, but it silently invalidated my first three searches for a c7-only shape — every candidate was failing c8 as well, so nothing ever showed up as "c7 alone." The fix was to embed the test symbol in a peer group (`synthetic.weak_peers()`). If you write a test that needs c8 to pass, it needs peers.
- **Changing the universe changes the screen** even though `config/screen.yaml` didn't move. Swapping S&P 500 for the Russell 3000 doesn't just add candidates; it re-ranks every existing one. Version the universe definition alongside the thresholds, and treat a universe change as a screen change for backtesting purposes.

---

## Implementation notes worth knowing

**ATR uses Wilder's smoothing, not a rolling mean.** A simple rolling mean drops the bar leaving the window, so ATR jumps when one old outlier expires. ATR sets stop distance and therefore position size — that discontinuity would show up as position sizes lurching for reasons unrelated to the stock. There's a test that fails if anyone "simplifies" it back.

**NaN never passes.** Every condition is `.fillna(False)`. An unknown and a failure are treated the same by the screen, but they're distinguished upstream: symbols without a full year of history are **skipped and counted**, not scored, so `NEWIPO` appears in `skipped_symbols` rather than as a rejection. Downstream code should never have to guess whether a name failed or was never evaluated.

**`as_of` truncates before anything is computed.** One line in `compute_symbol_metrics`, and it's what makes historical replay honest. `indicators.assert_no_lookahead()` proves that appending future bars doesn't change past metrics — run it in CI; it's cheap and it catches any future indicator that acquires a centred window.

**Ranking scores percentiles, not raw values.** A single 900%-slope outlier can't dominate the composite, and the weights in `config/screen.yaml` mean what they look like they mean. A missing metric scores 0, not 0.5 — absent evidence shouldn't earn a median score.

**Ordering is fully deterministic**: score, then RS rank, then symbol. Two runs of the same day produce byte-identical output, so journal entries stay comparable.

---

## What the funnel is for

An empty candidate list is a valid answer. The funnel is how you tell the two reasons apart:

- **c6/c7/c8 doing the killing** → the market genuinely has no Stage-2 leadership. Working as designed.
- **c1 killing everything, or survivor counts that never drop** → something upstream is broken. Stale cache, unadjusted prices after a split, a data provider returning garbage.

Read it on quiet days. It's the difference between trusting an empty list and wondering about it.

---

## Not built yet

`core/sizing.py` (ATR stops, position size against portfolio heat), `mcp/market_data/` (the Alpaca wrapper and cache), and `pipeline/stage_a_nightly.py` (the entry point that ties them together and writes `candidates.json`). The screen currently runs on synthetic data only — first contact with real Alpaca bars is where the IEX-volume caveat in `architecture.md` §15 gets tested.
