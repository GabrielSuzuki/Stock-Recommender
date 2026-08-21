"""Cost ledger: append-only JSONL, plus rollups.

Every optimized call writes one record with the *actual* billed usage and the
*counterfactual* baseline cost (what the same request would have cost on your
baseline model with no caching, compaction, or cache hits). Savings are the
difference, attributed per strategy so you can see which lever is paying.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Optional

from .pricing import cost

STRATEGIES = ("routing", "prompt_cache", "compaction", "semantic_cache")


@dataclass
class Record:
    ts: float
    request_id: str
    model: str
    baseline_model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    actual_cost: float = 0.0
    baseline_cost: float = 0.0
    saved: dict[str, float] = field(default_factory=dict)
    served_from_cache: bool = False
    escalated: bool = False
    latency_ms: float = 0.0
    route_reasons: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    tag: str = ""

    @property
    def total_saved(self) -> float:
        return sum(self.saved.values())


class Ledger:
    def __init__(self, path: str = ".tokenwise/ledger.jsonl", enabled: bool = True) -> None:
        self.path = path
        self.enabled = enabled
        self._lock = threading.Lock()
        if enabled:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    def write(self, record: Record) -> None:
        if not self.enabled:
            return
        with self._lock, open(self.path, "a") as fh:
            fh.write(json.dumps(asdict(record), default=str) + "\n")

    def read(self, since: Optional[float] = None) -> list[dict]:
        if not os.path.exists(self.path):
            return []
        out = []
        with open(self.path) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if since and rec.get("ts", 0) < since:
                    continue
                out.append(rec)
        return out

    # -- rollups -----------------------------------------------------------
    def summary(self, since: Optional[float] = None) -> dict[str, Any]:
        return summarize(self.read(since))


def summarize(records: Iterable[dict]) -> dict[str, Any]:
    records = list(records)
    n = len(records)
    if not n:
        return {"requests": 0}

    actual = sum(r.get("actual_cost", 0.0) for r in records)
    baseline = sum(r.get("baseline_cost", 0.0) for r in records)
    by_strategy = defaultdict(float)
    by_model: dict[str, dict[str, float]] = defaultdict(
        lambda: {"requests": 0, "cost": 0.0, "input": 0, "output": 0}
    )
    by_day: dict[str, dict[str, float]] = defaultdict(lambda: {"actual": 0.0, "baseline": 0.0, "requests": 0})

    cache_hits = escalations = 0
    tok_in = tok_out = tok_cw = tok_cr = 0
    latencies: list[float] = []

    for r in records:
        for k, v in (r.get("saved") or {}).items():
            by_strategy[k] += v
        m = r.get("model", "unknown")
        bm = by_model[m]
        bm["requests"] += 1
        bm["cost"] += r.get("actual_cost", 0.0)
        bm["input"] += r.get("input_tokens", 0) + r.get("cache_read_tokens", 0) + r.get("cache_write_tokens", 0)
        bm["output"] += r.get("output_tokens", 0)

        day = time.strftime("%Y-%m-%d", time.localtime(r.get("ts", time.time())))
        d = by_day[day]
        d["actual"] += r.get("actual_cost", 0.0)
        d["baseline"] += r.get("baseline_cost", 0.0)
        d["requests"] += 1

        cache_hits += bool(r.get("served_from_cache"))
        escalations += bool(r.get("escalated"))
        tok_in += r.get("input_tokens", 0)
        tok_out += r.get("output_tokens", 0)
        tok_cw += r.get("cache_write_tokens", 0)
        tok_cr += r.get("cache_read_tokens", 0)
        if r.get("latency_ms"):
            latencies.append(r["latency_ms"])

    saved = baseline - actual
    return {
        "requests": n,
        "actual_cost": actual,
        "baseline_cost": baseline,
        "saved": saved,
        "saved_pct": (saved / baseline * 100.0) if baseline else 0.0,
        "by_strategy": dict(by_strategy),
        "by_model": {k: dict(v) for k, v in by_model.items()},
        "by_day": {k: dict(v) for k, v in sorted(by_day.items())},
        "cache_hit_rate": cache_hits / n,
        "escalation_rate": escalations / n,
        "tokens": {
            "input": tok_in,
            "output": tok_out,
            "cache_write": tok_cw,
            "cache_read": tok_cr,
        },
        "avg_latency_ms": (sum(latencies) / len(latencies)) if latencies else 0.0,
        "window": {
            "start": min(r.get("ts", 0) for r in records),
            "end": max(r.get("ts", 0) for r in records),
        },
    }


def baseline_cost_of(model: str, input_tokens: int, output_tokens: int) -> float:
    """What this many tokens would cost with no optimization at all."""
    return cost(model, input_tokens=input_tokens, output_tokens=output_tokens)
