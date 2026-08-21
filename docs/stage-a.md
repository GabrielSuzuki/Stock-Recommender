# Stage A — the nightly job

Runs at **01:00 Pacific, Mon–Fri**. Refreshes bars, screens, ranks, sizes, writes `candidates.json`. No LLM, no cost beyond the VPS.

```bash
python3 -m pipeline.stage_a_nightly --offline       # synthetic data, no keys
python3 -m pipeline.stage_a_nightly                 # live (needs Alpaca keys)
python3 -m pipeline.stage_a_nightly --as-of 2026-06-15   # historical replay
```

It runs at 01:00 rather than 05:00 for one reason: if it fails there are four hours to notice before the brief is due. That margin is the entire argument for splitting the pipeline.

---

## Data flow

```
load_universe()          S&P 500 from Wikipedia, cached weekly to CSV
      ↓
get_bars()               SQLite cache; fetches only the delta
      ↓                  split detection, staleness assertion
build_metrics_frame()    per-symbol indicators, then cross-sectional RS rank
      ↓
evaluate()               2 gates + 8 Trend Template conditions
      ↓
top_candidates()         weighted percentile composite, capped at 20
      ↓
size_candidates()        ATR stop, R target, four caps, portfolio heat
      ↓
candidates.json          the Stage A → Stage B contract
```

---

## Four sizing caps, and the one people leave out

Every position is bounded by whichever of these binds first. `binding_cap` in the output tells you which one did.

| Cap | Rule | Config |
|---|---|---|
| **risk** | lose `risk_per_trade_pct` of equity if stopped out | `risk_per_trade_pct: 0.75` |
| **notional** | no single name above a share of equity | `max_position_pct: 25.0` |
| **liquidity** | no more than a share of 50-day average volume | `max_participation_pct: 0.5` |
| **heat** | total open risk across all positions | `max_portfolio_heat_pct: 4.0` |

The liquidity cap is the one that gets omitted. A position you can't exit in a day isn't a position — and with a 400k-share floor and a small account it will occasionally bind, which is the point of having it.

The heat cap **trims rather than rejects**: a partial position that fits the remaining risk budget beats skipping your best-ranked name because it didn't fit at full size.

Stops are ATR-based rather than a fixed percentage, so they adapt to the stock's own volatility — a 2% stop is loose on a utility and suicidal on a biotech.

---

## Three things building this turned up

### 1. Missing sector data silently capped the book at two positions

With no sector feed, every candidate falls into `UNKNOWN`, and `max_same_sector: 2` then rejects everything after the second name — reporting `"max_same_sector reached"`, which is not true. The first offline run took 2 positions instead of 6 and looked entirely plausible.

The fix: **the concentration cap is not enforced when the sector is unknown.** Refusing to constrain on a value you don't have is the lesser error, and `sectors_unknown` in the portfolio summary makes the missing protection visible instead of silent. There's a regression test.

The general shape of this bug is worth remembering: a default value that flows into a grouping key turns a missing feed into a silent policy change.

### 2. `candidates.json` has to be internally consistent, not just accurate

Prices were being rounded to four decimals on write while `risk_dollars` came from full-precision internals — so `shares × (entry − stop)` didn't reproduce the risk figure in the file. Off by less than a cent, and completely corrosive: a brief whose own arithmetic doesn't tie is one you start double-checking and then stop trusting.

Now entry, stop and target serialise at **two decimals** (they're order prices — that's what you can actually enter), and risk and notional are derived from those rounded values.

### 3. "Stale" has to mean the previous *session*, not the previous *day*

Stage A runs at 01:00, so the newest complete session is always the previous business day. Treating Thursday's close as stale on Friday morning would re-request the whole universe every morning for nothing. The freshness check ignores market holidays deliberately — treating a holiday as stale costs one wasted API call, while treating a real gap as fresh would screen on old prices. Wrong in the cheap direction.

---

## Failure behaviour

| Situation | What happens |
|---|---|
| One symbol's fetch fails | Logged, counted, run continues. Reported in `data_failures`. |
| Newest bar older than 5 days | **`StaleDataError`, run aborts.** Screening on stale prices produces a brief that looks normal and is wrong. |
| A split restates history | Detected by comparing overlapping bars; the symbol's cache is dropped and refetched in full |
| Anything uncaught | Telegram alert at 01:00, four hours before the brief |
| Nothing passes the screen | Not a failure. `candidates: []` plus the funnel explaining what killed what. |

`candidates.json` is written atomically — a half-written file read by Stage B at 05:00 would be worse than no file, because it would parse.

---

## The IEX volume problem, still unresolved

Alpaca's free feed is IEX-only. IEX is one venue with a low-single-digit share of consolidated tape, so `volume` comes back as roughly 2–5% of true traded volume, varying by symbol.

`min_avg_volume: 400000` is calibrated for **consolidated** volume. On this feed it will reject nearly everything.

`AlpacaProvider(volume_scale=...)` exists for this. **Measure it before trusting the gate** — pull a handful of names whose real ADV you can check, compute the ratio, and either set `volume_scale` or lower the floor. This is the single most likely thing to be wrong on first contact with live data, and it fails in the quiet direction: an empty candidate list that looks like a slow market.

---

## Output

`candidates.json` carries more than the winners — the funnel, the rejected names with reasons, and the config that produced them. Stage B's brief is far more useful when it can say "nothing passed, and here's what killed it" than when it can only say "nothing passed".

```json
{
  "schema_version": 1,
  "run": {"stage": "A", "as_of": "2026-08-19", "duration_seconds": 23.7},
  "data": {"requested": 503, "refreshed": 503, "splits_detected": 1, "errors": 0},
  "screen": {"evaluated": 498, "passed_gates": 471, "passed_template": 14},
  "funnel": [{"condition": "c1_above_ma50", "eliminated": 216, "surviving": 255}, ...],
  "portfolio": {"positions": 6, "portfolio_heat_pct": 3.94, "sectors_unknown": 0},
  "candidates": [
    {"symbol": "NVDA", "rs_rank": 97.2, "atr_pct": 3.1,
     "plan": {"sizable": true, "entry": 182.50, "stop": 176.20, "target": 198.25,
              "shares": 29, "risk_dollars": 182.70, "binding_cap": null}}
  ]
}
```
