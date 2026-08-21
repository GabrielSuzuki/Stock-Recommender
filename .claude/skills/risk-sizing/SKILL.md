---
name: risk-sizing
description: Position sizing formulas — ATR stops, R-multiple targets, and the four caps. Use when touching core/sizing.py, changing risk parameters, or explaining why a position came out smaller than expected.
---

# Risk and position sizing

Deterministic. The model never picks a size.

## The formulas

```
stop     = entry − atr_stop_multiple × ATR(14)
target   = entry + (entry − stop) × target_r_multiple
shares   = floor(risk_dollars / (entry − stop))
risk_$   = account_equity × risk_per_trade_pct / 100
```

ATR uses **Wilder's smoothing**, not a rolling mean. A rolling mean drops the
bar leaving the window, so ATR jumps when one old outlier expires — and since
ATR sets stop distance and therefore size, that shows up as position sizes
lurching for reasons unrelated to the stock. There is a test that fails if
anyone "simplifies" it back.

## Four caps — the binding one wins

| Cap | Rule | Config |
|---|---|---|
| risk | lose `risk_per_trade_pct` of equity if stopped | 0.75% |
| notional | no single name above a share of equity | `max_position_pct` 25% |
| **liquidity** | ≤ `max_participation_pct` of 50-day ADV | 0.5% |
| heat | total open risk across all positions | `max_portfolio_heat_pct` 4% |

`binding_cap` in the output names the one that bit.

**The liquidity cap is the one people omit.** A position you cannot exit in a
day is not a position. On a 400k-share floor with a small account it will
occasionally bind, which is the point.

**Heat trims rather than rejects.** A partial position that fits the remaining
risk budget beats skipping your best-ranked name because it did not fit whole.

## The sector cap and its trap

`max_same_sector: 2`. **Not enforced when the sector is `UNKNOWN`.**

With no sector feed every candidate falls into one bucket and the cap silently
limits the book to two positions, reporting "max_same_sector reached" — which is
not true. Refusing to constrain on a value we do not have is the lesser error;
`sectors_unknown` in the portfolio summary surfaces the missing protection.

Generalise it: **a default value flowing into a grouping key turns a missing
feed into a silent policy change.** Watch for it anywhere else a default meets a
group-by.

## Regime adjustment

`core.regime.apply_regime` halves `risk_per_trade_pct` and caps positions at 3
in NEUTRAL. RISK_OFF never reaches sizing — Stage B stops first.

## Serialisation

Entry, stop and target serialise at **two decimals** — they are order prices.
Risk and notional are derived from those rounded values, so `shares × (entry −
stop)` reproduces the file's own risk figure exactly. A brief whose arithmetic
does not tie is one you start double-checking and then stop reading.
