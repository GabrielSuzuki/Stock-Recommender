"""Shared Claude client for Stage B, wrapped in tokenwise.

One factory so every agent in the pipeline gets the same cost controls, the
same ledger, and the same safety settings. Import `agent_for()`, never
construct TokenWiseAgent directly.

Read docs/cost-control.md before changing any knob here. In particular the
semantic cache is namespaced per trading day on purpose -- see `_namespace`.
"""

from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path

# vendor/ holds the reconstructed tokenwise package
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "vendor"))

from tokenwise import Config, TokenWiseAgent  # noqa: E402

def _default_data_dir() -> Path:
    """Shared with pipeline.paths so there is exactly one default.

    NOTE: `Path("") or fallback` does NOT work -- Path("") is Path(".") which is
    truthy, so the fallback never fires and everything lands in the current
    working directory. Test the environment string itself.
    """
    from pipeline.paths import DATA_DIR as _shared
    return _shared


_env = os.environ.get("SCREENER_DATA", "").strip()
DATA_DIR = Path(_env) if _env else _default_data_dir()

# Which tier each pipeline role should start on. The router may still escalate.
ROLE_TIERS = {
    "market-regime":    "mid",     # one call/day, reads ~10 numbers, must be right
    "catalyst-analyst": "cheap",   # ~20 calls/day, mostly extraction + a boolean
    "thesis-writer":    "strong",  # one call/day, the only real judgment call
    "brief-editor":     "cheap",   # compression, no judgment
    "journal-analyst":  "strong",  # once a week, reads a month of history
}


def ledger_path() -> str:
    """The tokenwise ledger location. Single source of truth.

    Both the writer (`agent_for`) and the reader (`pipeline.cost`) call this,
    because they previously resolved it independently and drifted apart.
    """
    override = os.environ.get("TOKENWISE_LEDGER", "").strip()
    return override or str(DATA_DIR / "tokenwise" / "ledger.jsonl")


def _namespace(role: str) -> str:
    """Semantic-cache namespace: role + trading date.

    This is the single most important line in this file. tokenwise's semantic
    cache matches prompts at 0.93 cosine similarity, and two consecutive days'
    catalyst prompts for the same ticker are far more similar than that -- only
    the prices and dates differ. Without a per-day namespace the cache would
    happily serve yesterday's analysis for today's setup, and it would look
    like a saving rather than the silent correctness bug it is.

    Scoping to the date means the cache only ever helps within a single
    morning (retries, a re-run after a crash, the same ticker surfacing from
    two screens). That is a much smaller win, and it is the only safe one.
    """
    return f"{role}:{date.today().isoformat()}"


def agent_for(role: str, *, dry_run: bool = False) -> TokenWiseAgent:
    """Build the cost-optimized client for one pipeline role."""
    if role not in ROLE_TIERS:
        raise ValueError(f"unknown role {role!r}; expected one of {sorted(ROLE_TIERS)}")

    enabled = os.environ.get("TOKENWISE_ENABLED", "1") == "1"

    cfg = Config(
        # -- routing ---------------------------------------------------------
        routing="heuristic" if enabled else "off",
        min_tier=ROLE_TIERS[role],
        baseline_model="claude-opus-5",   # the counterfactual the ledger prices against

        # -- prompt caching --------------------------------------------------
        # The big win. Every catalyst-analyst call shares the same system
        # prompt and the same swing-screen + risk-sizing skill text. That
        # prefix is written to cache once and read ~19 times at 0.1x.
        prompt_cache=enabled,
        cache_ttl="5m",
        message_breakpoints=2,

        # -- compaction ------------------------------------------------------
        # Stage B conversations are short, so this mostly matters for the
        # weekly journal-analyst, which reads a month of briefs.
        compaction=enabled,
        max_context_tokens=120_000,
        tool_result_max_tokens=2_000,

        # -- semantic cache --------------------------------------------------
        # Deliberately conservative. See _namespace() above.
        semantic_cache=enabled and role in ("catalyst-analyst", "brief-editor"),
        semantic_threshold=0.97,          # tighter than the 0.93 default
        semantic_ttl_seconds=6 * 3600,    # expires well before the next morning
        semantic_namespace=_namespace(role),
        semantic_cache_path=str(DATA_DIR / "tokenwise" / "semantic_cache.db"),

        # -- accounting ------------------------------------------------------
        ledger=True,
        ledger_path=ledger_path(),
        verbose=dry_run,
        max_tokens=2048,
    )

    # NOTE: the first positional argument of TokenWiseAgent is `client`, not
    # `config` -- the vendored README gets this wrong and the mistake fails
    # late, at the first request, with a confusing AttributeError.
    return TokenWiseAgent(config=cfg)


def daily_cost_report(since: str | None = None) -> dict:
    """Savings summary for the Telegram footer / weekly review."""
    from tokenwise import Ledger

    ledger = Ledger(path=ledger_path())
    return ledger.summary(since=since)
