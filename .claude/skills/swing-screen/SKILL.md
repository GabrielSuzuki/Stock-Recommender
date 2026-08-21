---
name: swing-screen
description: The canonical definition of the Trend Template screen — every condition, every threshold, why it is there, and what must never be changed. Use when modifying core/screen.py, config/screen.yaml, or reasoning about why a name did or did not pass.
---

# The swing screen

Single source of truth. If this and the code disagree, the code is wrong.

## Two gates, then eight conditions, strict AND

| | Condition | Config key |
|---|---|---|
| gate | close ≥ min_price | `universe.min_price` (5.00) |
| gate | 50-day avg volume ≥ floor | `universe.min_avg_volume` (400000) |
| c1 | close > 50-day MA | — |
| c2 | close > 150-day MA | — |
| c3 | close > 200-day MA | — |
| c4 | 50-day > 150-day | — |
| c5 | 150-day > 200-day | — |
| c6 | 200-day rising ≥ 21 bars | `trend_template.ma200_rising_days` |
| c7 | within 25% of 52-week high | `trend_template.max_pct_below_52wk_high` |
| c8 | RS rank > 70 | `trend_template.min_rs_rank` |

Gates are separated from the template so the funnel never reports "failed the
trend template" when a stock was merely too illiquid to trade.

## Things that are settled — do not relitigate

**No scored variant.** c1∧c4 ⟹ c2, and c2∧c5 ⟹ c3. The eight conditions are six
independent constraints plus two that can only fail when something else already
has. A "6 of 8" version would triple-count the moving-average stack. Proven in
`core/test_screen.py::test_template_conditions_are_not_independent`.

**An empty list is the answer, not a bug.** On a choppy day the screen returns
nothing and that is the screen working. Do not add a fallback that always
returns something.

**c7 earns its place, narrowly.** ~600 smooth drawdown-and-recovery shapes were
swept and none failed c7 alone. The shape that isolates it is a short isolated
spike (squeeze, buyout rumour, index add) that sets a high the stock never
revisits while barely moving the 200-day. Without c7 the screen buys it. See
`core/synthetic.py::spiked_then_faded`.

**RS rank is cross-sectional, so the universe is a screen parameter.**
`min_rs_rank: 70` admits roughly the top 30% of whatever is fed in. Changing the
universe re-ranks every existing name even with the YAML untouched. A universe
change must be treated as a screen change when backtesting. A one-symbol
universe always scores 50 and can never pass c8.

## Reading the funnel

`ScreenResult.funnel()` shows what each condition eliminated, in order.

- **c6/c7/c8 doing the killing** → the market genuinely has no Stage-2
  leadership. Working as designed.
- **c1 killing everything, or survivor counts that never drop** → something
  upstream is broken: stale cache, unadjusted prices after a split, a provider
  returning garbage.
- **c2–c5 always eliminating zero** → expected, see the redundancy note above.

## Changing a threshold

1. Edit `config/screen.yaml` only. Never a `.py` file.
2. Commit the diff — the YAML is the audit trail.
3. Re-run the walk-forward backtest before and after. A change justified by a
   single-split improvement is a change justified by noise.
