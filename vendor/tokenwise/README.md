# tokenwise

An agent that makes your Claude API calls cost less — and proves how much less.

`tokenwise` wraps your Anthropic client. Every request goes through four
optimizers, then gets billed twice: once for what you actually paid, once for
what the same request would have cost unoptimized. The difference is written to
an append-only ledger, attributed per strategy. The savings number is an audit,
not a marketing claim.

```
  requests            320
  baseline cost       $25.10
  actual cost         $3.29
  saved               $21.81  (86.9%)

  savings by strategy
    compaction             $13.50  ████████████████████████████
    routing                 $4.93  ██████████
    semantic_cache          $1.89  ███
    prompt_cache            $1.49  ███
```

## Install

```bash
pip install -e .            # core (numpy only)
pip install -e ".[anthropic]"   # + the Anthropic SDK
```

## Try it with no API key

```bash
tokenwise simulate -n 300
open tokenwise-dashboard.html
```

That runs a synthetic workload against a mock backend which honours
`cache_control` breakpoints the way the real API does, so the numbers are
structurally real even though no money moves.

## Use it

```python
from tokenwise import TokenWiseAgent, Config

agent = TokenWiseAgent(Config(baseline_model="claude-opus-5"))

print(agent.ask("Classify this ticket as billing, technical, or other: card declined"))
agent.print_summary()
```

Or as a near drop-in for the SDK — pass the model you *would* have used and let
the router decide whether it's warranted:

```python
resp = agent.messages.create(
    model="claude-opus-5",       # the baseline, not necessarily what gets called
    max_tokens=1024,
    system=BIG_SYSTEM_PROMPT,
    messages=conversation,
)
```

CLI:

```bash
tokenwise ask "summarize this changelog: ..."   # one-off, prints cost to stderr
tokenwise stats --since 7d                      # text report
tokenwise dashboard -o costs.html --open        # HTML dashboard
tokenwise pricing                               # the price table in use
```

## The four levers

### 1. Model routing
Most production traffic is classification, extraction, formatting, or short
factual answers — work a small model does perfectly. The router scores each
request on signals that correlate with needed capability (mechanical vs.
reasoning verbs, context size, tool count, conversation depth, requested output
length, whether extended thinking was asked for) and picks a tier.

```python
Config(routing="heuristic")   # free, offline, ~0 latency  (default)
Config(routing="classifier")  # one ~$0.0002 Haiku call grades difficulty 1-3
Config(routing="off")         # pass through untouched
```

Asking for extended thinking is treated as an explicit statement that the task
needs capability, and always routes to the strong tier.

**Safety net.** Route down without gambling: supply a validator and a failed
cheap answer is retried on the next tier up. The retry's cost is charged back
against routing savings, so the headline number stays honest.

```python
Config(escalate_on=lambda resp: "i'm not sure" in text_of(resp).lower())
```

### 2. Prompt caching
Cache reads cost 0.1× input; writes cost 1.25×. A cached prefix pays for itself
on the second request and saves 90% on every one after — but placing
breakpoints by hand is fiddly, so most teams never do it.

tokenwise places them mechanically: tools first (the most stable thing in any
request), then the system prompt, then up to two rolling breakpoints anchored on
assistant turns so a growing conversation always has a warm prefix. It refuses
to place a breakpoint whose *incremental* span is below the model's minimum
cacheable length (1024 tokens, 2048 on Haiku) — a span that short is a write
you'll never amortize. It never exceeds the API's four-breakpoint limit, and it
leaves your own `cache_control` markers alone if you've placed any.

```python
Config(cache_ttl="1h")   # 2x write cost, survives slower conversation cadences
```

### 3. Context compaction
In a long agent loop the input side dominates spend, because the whole
transcript is resent every turn — and most of that transcript is dead weight.
Four passes, cheapest and safest first:

- strip thinking blocks from turns that are long over
- middle-out truncate oversized tool results (keeps the head and tail, where the
  structure lives)
- dedupe identical repeated payloads — the same file read three times becomes
  one copy plus a pointer
- if still over budget, evict the middle of the conversation, optionally
  replacing it with a Haiku-written bridge summary

`tool_use`/`tool_result` pairing is repaired after any eviction, so you never get
a 400 from a dangling `tool_result`. The most recent turns are never touched.

### 4. Semantic response cache
The cheapest request is the one you never send. Two layers: an exact hash of the
canonicalized request, then cosine similarity over a request vector.

The default embedder is dependency-free and lexical — stemmed content words plus
character trigrams. It catches the same request reworded, reordered,
re-punctuated, or pluralized, which is most of the repetition in real traffic. It
does **not** know that "automobile" means "car". For true paraphrase matching,
pass a real model:

```python
from tokenwise.semantic_cache import sentence_transformer_embedder
Config(embedder=sentence_transformer_embedder())   # or voyage_embedder()
```

Rails, because a wrong cache hit is worse than a wasted token:

- **Identity guard.** Numbers, dates, emails, URLs, hashes and `ABC-123` keys are
  extracted from every request. Two requests that differ in any of them are never
  matched by similarity — a cache that answers "invoice 456" with the answer for
  "invoice 123" is the worst bug a cache can have, and a similarity score will
  happily wave it through.
- Off by default for requests with tools, `temperature > 0.3`, or streaming.
- Strict 0.93 default threshold; entries expire (24h default) and the store is capped.

## How savings are calculated

For each request, with baseline model **B**, actual model **M**, pre-compaction
input estimate **pre**, billed input **post**, and output **out**:

| Strategy | Attribution |
|---|---|
| compaction | `input_cost(B, pre) − input_cost(B, post)` |
| routing | `cost(B, post, out) − cost(M, post, out)` − retries − classifier calls |
| prompt_cache | `input_cost(M, post) − input_cost(M, actual cache split)` |
| semantic_cache | full baseline cost of a request that was never sent |

These sum **exactly** to `baseline_cost − actual_cost` — there's a test asserting
it to 1e-9. Output token counts are taken from the real `usage` object; only the
pre-compaction input estimate is estimated, and the baseline uses
`max(pre, post)` so compaction can never be credited with savings it didn't make.

A negative bar in the report is real information: it's a cache write that hasn't
been amortized yet, or an escalation that didn't pay off.

Prices live in `pricing.py`, verified against Anthropic's pricing page for
August 2026. Override without touching code:

```bash
TOKENWISE_PRICING=my_prices.json tokenwise stats
```

## Dashboard

`tokenwise dashboard` writes one self-contained HTML file — no CDN, no build
step, no browser storage. Spend over time against the counterfactual, savings by
strategy, spend by model, plus a table view and dark mode.

## What this does not do

- It doesn't make a small model smarter. Routing is a bet that a chunk of your
  traffic is easy; measure your escalation rate and raise `min_tier` if it's high.
- The default embedder is lexical, not semantic. See above.
- Compaction throws information away on purpose. Tune `max_context_tokens` and
  `tool_result_max_tokens` to your task, and keep `summarize_evicted=True` if
  losing the middle of a conversation would hurt.
- Token estimates on the input side are heuristic (~few % error). They affect
  routing and compaction decisions, never your bill.

## Tests

```bash
pip install -e ".[dev]" && pytest -q
```

MIT.
