---
name: market-data
description: How to call each data provider, the exact free-tier limits, the cache-first pattern, and what to do when a source is down. Use when touching mcp/market_data/, adding a provider, or debugging a data problem.
---

# Market data

Everything upstream calls `get_bars()` / `get_context()` and never a vendor SDK.
Provider churn is the highest maintenance cost in a system like this; this is
the seam that contains it.

## Free-tier limits (verified Aug 2026)

| Source | Free tier | Role |
|---|---|---|
| Alpaca | 200 req/min, 7+ yrs, **IEX feed only** | primary OHLCV |
| Finnhub | 60 req/min | company news, earnings calendar |
| SEC EDGAR | 10 req/s, **User-Agent required** | 8-K / S-1 / 424B filings |
| Marketaux | 100 req/day, **3 articles per call** | ticker-tagged sentiment |
| Tiingo | 500 symbols/mo, 50 req/hr | fallback EOD |

**Do not use yfinance in production** — effectively deprecated, unannounced
rate limits, no SLA. **IEX Cloud is sunset.**

EDGAR blocks undeclared automated access. `SEC_USER_AGENT` must be
`"Your Name your@email.com"` or you get nothing, silently.

## The cache-first pattern

```
nothing cached   → full history from lookback_days back
cached through D → request from D−5 (overlap for split detection)
already current  → no request at all
```

The five-bar overlap is what makes split detection possible. Without it we only
ever see new bars and could never notice the old ones were restated. A split
that goes undetected leaves pre-split prices in the cache forever and every
moving average silently becomes wrong.

**"Current" means having the previous *business day's* bar**, not today's.
Stage A runs at 01:00, so the newest complete session is always yesterday.
Market holidays are deliberately ignored — a wasted API call is cheaper than
screening on old prices.

## Two hard failures

- `StaleDataError` — newest bar older than 5 days. **Aborts.** Screening on
  stale prices produces a brief that looks normal and is wrong.
- Empty universe — aborts.

Everything else degrades: one symbol's fetch failing is logged, counted in
`BarBundle.stats["failures"]`, and the run continues.

## The IEX volume problem — UNRESOLVED

Alpaca's free feed reports IEX-only volume: roughly 2–5% of consolidated tape,
varying by symbol. `min_avg_volume: 400000` is calibrated for consolidated
volume, so on live data the gate will reject nearly everything.

`AlpacaProvider(volume_scale=...)` exists for this. **Measure it** against names
whose real ADV you can check before trusting the gate. This fails in the quiet
direction — an empty candidate list that reads as a slow market.

## Adding a provider

Implement `fetch(symbol, start, end) -> DataFrame` with columns
`open, high, low, close, volume`, a `DatetimeIndex`, split- and
dividend-adjusted. Register it in `resolve_provider`. Nothing else changes.
