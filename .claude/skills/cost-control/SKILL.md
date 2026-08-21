---
name: cost-control
description: The tokenwise role-to-model-tier map and the semantic-cache safety rules. Use when changing pipeline/llm.py, adding an agent, or investigating API spend.
---

# Cost control

`vendor/tokenwise/` wraps every model call. Wired in `pipeline/llm.py` via one
factory: `agent_for(role)`. Never construct `TokenWiseAgent` directly.

## Role → tier

| Role | Tier | Calls/day | Why |
|---|---|---|---|
| `market-regime` | mid | 1 | reads ~10 numbers, must be right |
| `catalyst-analyst` | cheap | ~20 | extraction plus a boolean |
| `thesis-writer` | strong | 1 | the only real judgement in the pipeline |
| `brief-editor` | cheap | 1 | one sentence |
| `journal-analyst` | strong | 1/week | reads a month of history |

Measured: **$24.52/mo unoptimized → $5.49/mo (77.6% saved)**. Routing is ~55% of
that, prompt caching ~20%.

## The dangerous default

> `semantic_threshold=0.93` with a 24-hour TTL and a static namespace will serve
> yesterday's analysis and book it as a saving.

Monday's catalyst prompt for AAPL and Tuesday's share a system prompt, a ticker,
and a news window — they differ only in a few prices and a date. Cosine
similarity sits comfortably above 0.93. The cache would return Monday's analysis
for Tuesday's setup, and the ledger would record it as money saved: a *better*
savings number for a *worse* brief.

`pipeline/llm.py` handles it three ways:

```python
semantic_cache=enabled and role in ("catalyst-analyst", "brief-editor"),
semantic_threshold=0.97,
semantic_ttl_seconds=6 * 3600,
semantic_namespace=f"{role}:{date.today().isoformat()}",
```

Off entirely for `thesis-writer` and `market-regime` — one call each per day,
nothing to save and everything to lose. Namespaced per trading day, so a hit is
only ever possible within one morning.

`pipeline/test_llm.py::test_semantic_cache_off_for_thesis_writer` fails if
anyone re-enables it globally.

**Watch `cache_hit_rate`.** Above ~15% on the catalyst role means something is
matching across days — check the namespace before celebrating.

## Cheapest savings, in order

1. Keep the deterministic layer deterministic. Still the largest single saving.
2. Mechanical disqualification before the model sees a name — drops 20–30% of
   candidates with nothing to reason about.
3. RISK_OFF stops the pipeline before any per-name call. A risk-off morning
   should be the cheapest of the month.
4. Cap candidates at 20 before Stage B starts.

## Kill switch

`TOKENWISE_ENABLED=0` disables routing, caching, compaction and the semantic
cache while leaving the ledger recording. If a brief looks wrong, flip it,
re-run, compare. That comparison is the only way to tell "the model was wrong"
from "the optimizer served me something stale".
