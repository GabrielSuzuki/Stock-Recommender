# Cost control — wiring tokenwise into Stage B

Your `tokenwise` package is the cost layer for the pipeline. This documents how it's wired, what it saves on this specific workload, and the one place where it will silently corrupt your brief if you leave the defaults on.

The package lives at `vendor/tokenwise/`. The integration is `pipeline/llm.py` — one factory, `agent_for(role)`, so every agent gets identical settings and writes to one ledger.

---

## 1. Reconstruction note

The upload arrived with shuffled filenames — every file's content belonged to a different module. Mapping recovered from the relative imports:

| Uploaded as | Actually |
|---|---|
| `caching.py` | `__init__.py` |
| `quickstart.py` | `agent.py` |
| `compaction.py` | `caching.py` |
| `README.md` | `compaction.py` |
| `router.py` | `config.py` |
| `pricing.py` | `router.py` |
| `long_agent_loop.py` | `semantic_cache.py` |
| `simulate.py` | `ledger.py` |
| `agent.py` | `pricing.py` |
| `ledger.py` | `tokens.py` |
| `dashboard.py` | `simulate.py` |
| `semantic_cache.py` | `dashboard.py` |
| `__init__.py` | `report.py` |
| `config.py` | `cli.py` |
| `test_tokenwise.py` | `README.md` |

No code was edited. `import tokenwise` succeeds, all 14 submodules import, and the mock simulation runs:

```
$ python3 -m tokenwise.cli simulate -n 300
  requests            300
  baseline cost       $23.42
  actual cost         $3.01
  saved               $20.41  (87.2%)
    compaction $12.62 | routing $4.63 | semantic_cache $1.78 | prompt_cache $1.38
```

**Two things missing from the upload**, both harmless for our purposes: the test file (`pytest` suite the README references) and `pyproject.toml`. Without the latter there's no `tokenwise` console script, so use `python3 -m tokenwise.cli`. The savings-accounting identity the tests were supposed to check — `sum(by_strategy) == baseline_cost - actual_cost` — was verified by hand against real ledger output and holds to 3.6e-15.

**One bug in the vendored README:** it shows `TokenWiseAgent(Config(...))`, but the first positional parameter is `client`. That constructs fine and then dies at the first request with a confusing `AttributeError: 'Config' object has no attribute 'messages'`. `pipeline/llm.py` uses the keyword form and carries a comment saying why.

**Pricing table: verified correct.** I checked all 15 entries in `pricing.py` against the published Claude pricing page (Aug 2026). Every input, output, cache-write and cache-read figure matches, including `claude-fable-5` and `claude-mythos-5` at $10/$50 — those are real models, not placeholders. Your counterfactual numbers can be trusted.

---

## 2. What it costs — MEASURED, 2026-08-20

First live Stage B against real S&P 500 candidates, 22 requests:

| | Per day | Per month (21 sessions) |
|---|---|---|
| Baseline — everything on Opus 5 | $0.173 | $3.63 |
| Actual | **$0.079** | **$1.66** |
| Saved | $0.094 | $1.97 (**54.3%**) |

That is roughly a third of the $5.49/mo this document originally projected. The
estimate was not wrong about the model or the routing — it was wrong about the
**prompt size**, because it assumed a news feed that was not connected. Each
catalyst call measured ~406 input tokens against an assumed ~7,000.

**Expect ~$2.45/mo once Finnhub is connected** and each catalyst prompt carries
eight headlines. Still under a fifth of the VPS bill.

### Where the saving comes from — measured

| Lever | Saved | Share |
|---|---|---|
| routing | $0.0936 | **99.9%** |
| compaction | $0.000105 | 0.1% |
| prompt_cache | $0.000000 | 0% |
| semantic_cache | $0.000000 | 0% |

**Routing is doing essentially all the work**: 21 of 22 calls went to Haiku 4.5
at $0.0234 total, while the single Opus thesis call cost $0.0556 on its own.
One call is 70% of the bill, and it is the one worth paying for.

### prompt_cache reporting zero is correct, not broken

`cache-write 0, cache-read 0`. Anthropic requires a minimum block size before
anything is cached — **2048 tokens on Haiku 4.5**, 1024 on the larger models.
The entire catalyst prompt is ~406 tokens, so no cache block is ever created.

The lever cannot engage at this prompt size, and that is fine. Do not pad the
system prompt to reach the threshold: you would pay for the padding on every
single call to save 90% on a prefix that is cheap precisely because it is
small. Revisit only if the shared prefix genuinely grows past 2k tokens.

Compaction is similarly near-zero because Stage B conversations are single-turn.
It earns its keep on the weekly `journal-analyst`, which reads a month of history.

## 3. The dangerous default

> **`semantic_threshold=0.93` with a 24-hour TTL and a static namespace will serve you yesterday's analysis and book it as a saving.**

This is the one thing to get right. tokenwise's semantic cache matches prompts by embedding similarity. In a general workload that's exactly right — "what's your refund policy" and "how do refunds work" *are* the same request.

In a daily financial pipeline it's a trap. Monday's catalyst prompt for AAPL and Tuesday's are the same system prompt, the same ticker, the same skill text, and the same news window — differing only in a few price numbers and a date. Cosine similarity between them is comfortably above 0.93. The cache would return Monday's analysis for Tuesday's setup, and the ledger would record it as money saved. You'd see a *better* savings number for a *worse* brief, which is the hardest class of bug to notice.

`pipeline/llm.py` handles this three ways:

```python
semantic_cache=enabled and role in ("catalyst-analyst", "brief-editor"),
semantic_threshold=0.97,          # tighter than the 0.93 default
semantic_ttl_seconds=6 * 3600,    # expires before the next morning
semantic_namespace=f"{role}:{date.today().isoformat()}",
```

1. **Off entirely for `thesis-writer` and `market-regime`.** These two must never be served a cached answer. They're one call each per day — there's nothing to save and everything to lose.
2. **Namespaced per trading day.** The cache can only ever hit within a single morning.
3. **Threshold raised and TTL cut**, as defence in depth.

What's left is the only safe win: retries after a crash, a re-run of the same morning, or a ticker surfacing twice from different screens. Small. Correct.

There's a test for this in `pipeline/test_llm.py`:

```python
def test_semantic_cache_off_for_thesis_writer(self):
    self.assertFalse(agent_for("thesis-writer").semantic.enabled)
    self.assertFalse(agent_for("market-regime").semantic.enabled)
```

If someone later "optimizes" by turning it on globally, that test fails.

---

## 4. Role → tier map

From `pipeline/llm.py`:

| Role | Tier | Model | Calls/day | Why |
|---|---|---|---|---|
| `market-regime` | mid | Sonnet 5 | 1 | Reads ~10 numbers and must be right — it can cancel the whole brief |
| `catalyst-analyst` | cheap | Haiku 4.5 | ~20 | Extraction plus a boolean. The bulk of the volume. |
| `thesis-writer` | strong | Opus 5 | 1 | The only real judgment call in the pipeline |
| `brief-editor` | cheap | Haiku 4.5 | 1 | Compression, no judgment |
| `journal-analyst` | strong | Opus 5 | 1/week | Reads a month of history |

`min_tier` sets the floor; the heuristic router may still send something harder upward. `baseline_model="claude-opus-5"` is the counterfactual the ledger prices everything against — it's what the ledger pretends you would have paid.

---

## 5. Reading the savings

```python
from pipeline.llm import daily_cost_report
print(daily_cost_report(since="7d"))
```

Returns `requests, actual_cost, baseline_cost, saved, saved_pct, by_strategy, by_model, by_day, cache_hit_rate, escalation_rate, tokens{...}, avg_latency_ms`.

Two things worth doing with it:

- **One line in the Telegram brief footer**, e.g. `spend $0.24 · saved 78%`. Costs nothing and means you'd notice a 10x spike the morning it happens rather than at the end of the month.
- **The HTML dashboard**, weekly, from the `journal-analyst`:
  ```bash
  python3 -m tokenwise.cli dashboard --out /opt/screener/logs/tokenwise.html
  ```
  Self-contained, ~22 KB, no CDN.

**Watch `cache_hit_rate`.** If it climbs above roughly 15% on the catalyst role, something is matching across days that shouldn't be — check the namespace before you celebrate.

---

## 6. Kill switch

```bash
TOKENWISE_ENABLED=0
```

Turns off routing, caching, compaction and the semantic cache in one move, leaving the ledger recording. If a brief ever looks wrong, flip this, re-run, and compare. That comparison — same day, optimizers off — is the only way to tell "the model was wrong" from "the optimizer served me something stale."

---

## 7. Deployment

`vendor/tokenwise/` is committed, so `deploy.sh` ships it. On the VPS:

```bash
export PYTHONPATH=/opt/screener/app/vendor:$PYTHONPATH
```

Already handled by `pipeline/llm.py`, which prepends `vendor/` to `sys.path`. Only `numpy` is required beyond stdlib, plus `anthropic` for live calls.

Ledger and cache live under `/opt/screener/data/tokenwise/`, which is in the systemd `ReadWritePaths` allowlist. If you move them, update `screener-brief.service` or the write will fail under `ProtectSystem=strict`.
