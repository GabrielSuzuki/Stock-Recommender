"""Cheap, offline token estimation.

We deliberately avoid calling the count_tokens endpoint on the hot path: it is
a network round trip per request and the optimizers only need estimates good
to within a few percent to make routing/compaction decisions. Real token
counts always come back from the API in `usage` and are what the ledger bills.
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterable

# Empirically ~3.6 chars/token for English prose, ~2.8 for code/JSON.
_WORDish = re.compile(r"[A-Za-z]{2,}")


def estimate_text(text: str) -> int:
    if not text:
        return 0
    n = len(text)
    # Denser (code, JSON, ids) text tokenizes worse than prose.
    alpha = len(_WORDish.findall(text))
    prose_ratio = min(1.0, (alpha * 5) / max(n, 1))
    chars_per_token = 2.8 + 0.9 * prose_ratio
    return max(1, int(n / chars_per_token))


def estimate_block(block: Any) -> int:
    """Estimate tokens for one content block (dict, str, or SDK object)."""
    if isinstance(block, str):
        return estimate_text(block)
    if not isinstance(block, dict):
        block = getattr(block, "model_dump", lambda: {"text": str(block)})()

    btype = block.get("type")
    if btype == "text":
        return estimate_text(block.get("text", ""))
    if btype == "image":
        src = block.get("source", {}) or {}
        data = src.get("data") or ""
        if data:
            # ~1.37 tokens per 750 base64 chars is a decent stand-in for
            # (w*h)/750 on typical screenshots.
            return max(1, int(len(data) * 0.75 / 750))
        return 1200  # URL image, unknown size: assume a large-ish screenshot
    if btype in ("tool_use", "server_tool_use"):
        return estimate_text(json.dumps(block.get("input", {}))) + 10
    if btype == "tool_result":
        content = block.get("content")
        if isinstance(content, str):
            return estimate_text(content) + 5
        if isinstance(content, list):
            return sum(estimate_block(b) for b in content) + 5
        return 5
    if btype == "thinking":
        return estimate_text(block.get("thinking", ""))
    return estimate_text(json.dumps(block, default=str))


def estimate_message(message: Any) -> int:
    if isinstance(message, str):
        return estimate_text(message)
    content = message.get("content") if isinstance(message, dict) else getattr(message, "content", "")
    if isinstance(content, str):
        return estimate_text(content) + 4
    if isinstance(content, Iterable):
        return sum(estimate_block(b) for b in content) + 4
    return 4


def estimate_messages(messages: Iterable[Any]) -> int:
    return sum(estimate_message(m) for m in messages)


def estimate_system(system: Any) -> int:
    if not system:
        return 0
    if isinstance(system, str):
        return estimate_text(system)
    return sum(estimate_block(b) for b in system)


def estimate_tools(tools: Any) -> int:
    if not tools:
        return 0
    total = 0
    for tool in tools:
        d = tool if isinstance(tool, dict) else getattr(tool, "model_dump", lambda: {})()
        total += estimate_text(json.dumps(d, default=str))
    return total + 20  # tool-use system overhead
