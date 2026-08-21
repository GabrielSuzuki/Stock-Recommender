"""Configuration for the agent. Every knob has a sane default."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .router import DEFAULT_TIERS


@dataclass
class Config:
    # --- routing
    routing: str = "heuristic"          # heuristic | classifier | off
    tiers: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_TIERS))
    baseline_model: str = "claude-opus-5"
    min_tier: str = "cheap"
    escalate_on: Optional[Callable[[Any], bool]] = None   # return True to retry higher

    # --- prompt caching
    prompt_cache: bool = True
    cache_ttl: str = "5m"               # 5m | 1h
    message_breakpoints: int = 2

    # --- compaction
    compaction: bool = True
    max_context_tokens: int = 120_000
    tool_result_max_tokens: int = 2_000
    keep_head: int = 2
    keep_tail: int = 6
    dedupe: bool = True
    drop_thinking: bool = True
    summarize_evicted: bool = True      # uses the cheap tier to write the bridge summary

    # --- semantic cache
    semantic_cache: bool = True
    semantic_threshold: float = 0.93
    semantic_ttl_seconds: int = 86_400
    semantic_cache_path: str = ".tokenwise/semantic_cache.db"
    semantic_namespace: str = "default"
    embedder: Optional[Callable[[str], Any]] = None

    # --- accounting
    ledger_path: str = ".tokenwise/ledger.jsonl"
    ledger: bool = True
    verbose: bool = False

    # --- request defaults
    max_tokens: int = 1024
