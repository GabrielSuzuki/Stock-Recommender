"""A mock Anthropic backend + synthetic workload.

Lets you see the savings, and sanity-check the accounting, without spending a
cent or needing an API key. The mock emulates the parts that matter for cost:
it honours cache_control breakpoints (charging a write the first time it sees
a prefix and a read afterwards) and reports usage the same shape the real API
does.
"""

from __future__ import annotations

import hashlib
import os
import random
import time
from typing import Any

from . import dashboard, report, tokens
from .agent import TokenWiseAgent
from .config import Config
from .ledger import Ledger


class MockUsage(dict):
    def __getattr__(self, k: str) -> Any:  # allow both attr and item access
        try:
            return self[k]
        except KeyError as exc:
            raise AttributeError(k) from exc


class MockResponse:
    def __init__(self, text: str, model: str, usage: dict) -> None:
        self.content = [{"type": "text", "text": text}]
        self.model = model
        self.role = "assistant"
        self.stop_reason = "end_turn"
        self.usage = MockUsage(usage)


class MockMessages:
    def __init__(self, client: "MockClient") -> None:
        self._c = client

    def create(self, **kw: Any) -> MockResponse:
        return self._c._create(**kw)


class MockClient:
    """Emulates enough of the Messages API to exercise the optimizers."""

    def __init__(self, seed: int = 7, cache_ttl: float = 300.0) -> None:
        self.messages = MockMessages(self)
        self.rng = random.Random(seed)
        self.cache: dict[str, float] = {}
        self.cache_ttl = cache_ttl
        self.calls = 0

    def _create(self, **kw: Any) -> MockResponse:
        self.calls += 1
        model = kw["model"]
        msgs = kw.get("messages", [])
        system = kw.get("system")
        tools = kw.get("tools")

        total = (
            tokens.estimate_messages(msgs)
            + tokens.estimate_system(system)
            + tokens.estimate_tools(tools)
        )
        breakpoints = _cached_prefixes(msgs, system, tools)

        # The real API caches at every breakpoint independently: the longest
        # warm prefix is read, and everything from there to the last
        # breakpoint is written. Model that, or the mock will slander caching.
        cache_read = cache_write = 0
        if breakpoints:
            now = time.time()
            warm = [t for t, k in breakpoints
                    if (h := self.cache.get(k)) is not None and now - h < self.cache_ttl]
            cache_read = max(warm) if warm else 0
            cache_write = max(t for t, _ in breakpoints) - cache_read
            for t, k in breakpoints:
                self.cache[k] = now

        billed_input = max(0, total - cache_read - cache_write)
        max_tokens = kw.get("max_tokens", 1024)
        out = max(4, min(max_tokens, int(self.rng.gauss(max_tokens * 0.35, max_tokens * 0.12))))

        body = f"[mock:{model}] " + "answer " * max(1, out // 4)
        return MockResponse(
            body,
            model,
            {
                "input_tokens": billed_input,
                "output_tokens": out,
                "cache_creation_input_tokens": cache_write,
                "cache_read_input_tokens": cache_read,
            },
        )


def _cached_prefixes(msgs: list, system: Any, tools: Any) -> list[tuple[int, str]]:
    """Every cache_control breakpoint as (tokens covered, prefix identity)."""
    running = 0
    out: list[tuple[int, str]] = []
    key_src: list[str] = []

    for group, est in ((tools, tokens.estimate_tools), (system, tokens.estimate_system)):
        if not group:
            continue
        running += est(group)
        if isinstance(group, list) and any(
            isinstance(b, dict) and b.get("cache_control") for b in group
        ):
            key_src.append(str(group))
            out.append((running, hashlib.sha1("|".join(key_src).encode()).hexdigest()))

    for m in msgs:
        running += tokens.estimate_message(m)
        content = m.get("content") if isinstance(m, dict) else getattr(m, "content", None)
        if isinstance(content, list) and any(
            isinstance(b, dict) and b.get("cache_control") for b in content
        ):
            key_src.append(str(m)[:400])
            out.append((running, hashlib.sha1("|".join(key_src).encode()).hexdigest()))

    return out


# --- the synthetic workload ------------------------------------------------
FAQ = [
    "What is your refund policy?",
    "How do refunds work here?",
    "Can I get my money back?",
    "How long does shipping take?",
    "When will my order arrive?",
    "Do you ship internationally?",
]
SIMPLE = [
    "Classify this support ticket as billing, technical, or other: {x}",
    "Extract every email address from the following text: {x}",
    "Summarize this paragraph in one sentence: {x}",
    "Translate to Spanish: {x}",
    "Rewrite this more concisely: {x}",
]
HARD = [
    "Design a schema for multi-tenant usage metering with per-second granularity, and explain the trade-offs of each index you add. {x}",
    "Debug this deadlock: two workers take locks in opposite order under retry. Walk through the root cause step by step. {x}",
    "Prove that the greedy interval-scheduling algorithm is optimal, then analyze its complexity. {x}",
]
FILLER = (
    "Lorem ipsum dolor sit amet, consectetur adipiscing elit, sed do eiusmod tempor "
    "incididunt ut labore et dolore magna aliqua. Ut enim ad minim veniam, quis nostrud. "
)

BIG_SYSTEM = (
    "You are a support agent for Acme Corp.\n"
    + ("Policy detail. " + FILLER) * 60
)

TOOLS = [
    {
        "name": f"tool_{i}",
        "description": "Look things up in the internal knowledge base. " + FILLER,
        "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}},
    }
    for i in range(6)
]


def run(
    n: int = 200,
    ledger_path: str = ".tokenwise/ledger.jsonl",
    out: str = "tokenwise-dashboard.html",
    spread_days: int = 14,
) -> dict:
    rng = random.Random(11)
    client = MockClient()

    # Fresh ledger + cache so the numbers describe this run only.
    for path in (ledger_path, ".tokenwise/simulate_cache.db"):
        if os.path.exists(path):
            os.remove(path)

    agent = TokenWiseAgent(
        client=client,
        config=Config(
            ledger_path=ledger_path,
            semantic_cache_path=".tokenwise/simulate_cache.db",
            semantic_namespace="simulate",
            baseline_model="claude-opus-5",
            max_context_tokens=25_000,
            tool_result_max_tokens=1_500,
            summarize_evicted=False,
        ),
    )

    print(f"simulating {n} requests against a mock backend…")
    for i in range(n):
        roll = rng.random()
        if roll < 0.30:  # repeat FAQ traffic
            agent.create(
                model="claude-opus-5",
                max_tokens=400,
                system=BIG_SYSTEM,
                messages=[{"role": "user", "content": rng.choice(FAQ)}],
                tag="faq",
            )
        elif roll < 0.70:  # mechanical work, routable to a small model
            agent.create(
                model="claude-opus-5",
                max_tokens=512,
                system=BIG_SYSTEM,
                messages=[
                    {
                        "role": "user",
                        "content": rng.choice(SIMPLE).format(
                            x=f"[case {rng.randrange(10**6)}] " + FILLER * rng.randint(1, 6)
                        ),
                    }
                ],
                tag="simple",
            )
        elif roll < 0.88:  # long agent loop with bloated tool results
            agent.create(
                model="claude-opus-5",
                max_tokens=2048,
                system=BIG_SYSTEM,
                tools=TOOLS,
                messages=_agent_transcript(rng),
                tag="agent-loop",
            )
        else:  # genuinely hard reasoning — should stay on the big model
            agent.create(
                model="claude-opus-5",
                max_tokens=4096,
                system=BIG_SYSTEM,
                messages=[
                    {
                        "role": "user",
                        "content": rng.choice(HARD).format(
                            x=f"[incident {rng.randrange(10**6)}] " + FILLER * 3
                        ),
                    }
                ],
                tag="hard",
            )
        if (i + 1) % 50 == 0:
            print(f"  {i + 1}/{n}")

    if spread_days > 1:
        _spread_over_days(ledger_path, spread_days)

    summary = Ledger(ledger_path).summary()
    summary["semantic_cache"] = agent.semantic.stats()
    agent.close()

    print()
    print(report.render_text(summary))
    dashboard.write(summary, out, baseline_model="claude-opus-5",
                    title="tokenwise — simulated workload")
    print(f"\nwrote {out}  ({client.calls} calls actually sent to the backend)")
    return summary


def _spread_over_days(ledger_path: str, days: int) -> None:
    """Backdate the simulated run across a date range so the time series has
    something to show. Only ever applied to simulated data."""
    import json

    with open(ledger_path) as fh:
        rows = [json.loads(line) for line in fh if line.strip()]
    if not rows:
        return
    end = time.time()
    span = days * 86_400
    for i, row in enumerate(rows):
        row["ts"] = end - span + (i / max(1, len(rows) - 1)) * span
    with open(ledger_path, "w") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


def _agent_transcript(rng: random.Random) -> list[dict]:
    """A tool-using conversation with the usual pathologies: giant tool
    results, the same file read twice, stale thinking blocks."""
    big = FILLER * 220
    msgs: list[dict] = [
        {"role": "user", "content": f"Investigate failing checkout run {rng.randrange(10**6)}."}
    ]
    for k in range(rng.randint(4, 9)):
        msgs.append(
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "Considering next step. " + FILLER * 8},
                    {"type": "tool_use", "id": f"t{k}", "name": "tool_0", "input": {"q": f"step {k}"}},
                ],
            }
        )
        msgs.append(
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": f"t{k}", "content": big},
                ],
            }
        )
    msgs.append({"role": "user", "content": "What did you find? Give me the fix."})
    return msgs
