"""Automatic prompt-cache breakpoint placement.

Cache reads cost 0.1x input; cache writes cost 1.25x. So a cached prefix pays
for itself on the *second* request that reuses it and saves 90% on every one
after. Most teams never place breakpoints because doing it by hand is fiddly
and easy to invalidate. This module does it mechanically.

Placement rules:
  1. Prefix order in the API is tools -> system -> messages. A breakpoint
     caches everything before it, so we walk that order.
  2. Never place a breakpoint unless the *incremental* span since the previous
     one clears the model's minimum cacheable length -- a short span is a
     write you'll never amortize.
  3. Keep two rolling breakpoints in the message list so a growing
     conversation always has a warm prefix while the newer one is written.
  4. Max 4 breakpoints (API limit). Existing caller breakpoints are respected.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Optional

from . import tokens

MAX_BREAKPOINTS = 4

# Minimum cacheable prompt length, in tokens, per model family.
MIN_CACHEABLE = {
    "claude-haiku-3-5": 2048,
    "claude-haiku-4-5": 2048,
}
DEFAULT_MIN_CACHEABLE = 1024


def min_cacheable(model: str) -> int:
    from .pricing import normalize

    return MIN_CACHEABLE.get(normalize(model), DEFAULT_MIN_CACHEABLE)


@dataclass
class CacheReport:
    breakpoints: list[str] = field(default_factory=list)
    cached_prefix_tokens: int = 0
    skipped: list[str] = field(default_factory=list)
    caller_managed: bool = False


def _has_cache_control(blocks: Any) -> bool:
    if not isinstance(blocks, list):
        return False
    return any(isinstance(b, dict) and b.get("cache_control") for b in blocks)


def _mark(block: dict, ttl: str) -> None:
    cc: dict[str, Any] = {"type": "ephemeral"}
    if ttl != "5m":
        cc["ttl"] = ttl
    block["cache_control"] = cc


def _as_blocks(content: Any) -> Optional[list]:
    """Return content as a mutable block list, or None if not markable."""
    if isinstance(content, list):
        return content
    return None


class CacheOptimizer:
    """Inserts cache_control breakpoints into a request.

    Args:
        ttl: "5m" or "1h". 1h costs 2x input to write but survives slower
            conversation cadences -- worth it for long-lived agent sessions.
        message_breakpoints: how many rolling breakpoints to keep in the
            message list (0 disables conversation caching).
    """

    def __init__(self, ttl: str = "5m", message_breakpoints: int = 2, enabled: bool = True) -> None:
        if ttl not in ("5m", "1h"):
            raise ValueError("ttl must be '5m' or '1h'")
        self.ttl = ttl
        self.message_breakpoints = max(0, min(2, message_breakpoints))
        self.enabled = enabled

    def apply(
        self,
        model: str,
        messages: list[Any],
        system: Any = None,
        tools: Any = None,
    ) -> tuple[list[Any], Any, Any, CacheReport]:
        report = CacheReport()
        if not self.enabled:
            report.skipped.append("disabled")
            return messages, system, tools, report

        floor = min_cacheable(model)

        # Respect caller-placed breakpoints rather than fighting them.
        if _has_cache_control(system) or _has_cache_control(tools) or any(
            _has_cache_control(_content_of(m)) for m in messages
        ):
            report.caller_managed = True
            report.skipped.append("caller-placed-breakpoints")
            return messages, system, tools, report

        messages = copy.deepcopy(messages)
        system = copy.deepcopy(system)
        tools = copy.deepcopy(tools)

        budget = MAX_BREAKPOINTS
        prefix = 0  # tokens covered by the last placed breakpoint

        # 1) tools -- the most stable thing in the whole request
        tool_tokens = tokens.estimate_tools(tools)
        if tools and budget and tool_tokens >= floor:
            last = tools[-1]
            if isinstance(last, dict):
                _mark(last, self.ttl)
                budget -= 1
                prefix = tool_tokens
                report.breakpoints.append("tools")
        elif tools:
            report.skipped.append(f"tools({tool_tokens}t < {floor})")

        # 2) system prompt
        sys_tokens = tokens.estimate_system(system)
        if isinstance(system, str) and sys_tokens:
            # Promote to block form so it can carry cache_control.
            system = [{"type": "text", "text": system}]
        if isinstance(system, list) and system and budget:
            if tool_tokens + sys_tokens - prefix >= floor:
                _mark(system[-1], self.ttl)
                budget -= 1
                prefix = tool_tokens + sys_tokens
                report.breakpoints.append("system")
            else:
                report.skipped.append(f"system({sys_tokens}t incremental < {floor})")

        # 3) rolling conversation breakpoints
        placed = 0
        if budget and self.message_breakpoints and messages:
            for idx in _rolling_indices(messages, self.message_breakpoints):
                upto = prefix + tokens.estimate_messages(messages[: idx + 1])
                if upto - prefix < floor:
                    report.skipped.append(f"messages[:{idx + 1}]({upto - prefix}t < {floor})")
                    continue
                blocks = _as_blocks(_content_of(messages[idx]))
                if blocks is None:
                    blocks = [{"type": "text", "text": str(_content_of(messages[idx]))}]
                    _set_content(messages[idx], blocks)
                if not isinstance(blocks[-1], dict):
                    continue
                _mark(blocks[-1], self.ttl)
                budget -= 1
                placed += 1
                prefix = upto
                report.breakpoints.append(f"messages[{idx}]")
                if budget == 0 or placed >= self.message_breakpoints:
                    break

        report.cached_prefix_tokens = prefix
        return messages, system, tools, report


def _rolling_indices(messages: list[Any], want: int) -> list[int]:
    """Breakpoint candidates: end of the stable prefix, newest first.

    We anchor on assistant turns because everything up to and including an
    assistant reply is immutable for the rest of the conversation.
    """
    assistants = [i for i, m in enumerate(messages) if _role_of(m) == "assistant"]
    if not assistants:
        # Single-shot request: cache everything except the final user turn only
        # if there is something before it.
        return [len(messages) - 2] if len(messages) >= 2 else []
    picks = [assistants[-1]]
    if want > 1 and len(assistants) >= 3:
        picks.append(assistants[len(assistants) // 2])
    return picks[:want]


def _content_of(m: Any) -> Any:
    return m.get("content") if isinstance(m, dict) else getattr(m, "content", None)


def _set_content(m: Any, value: Any) -> None:
    if isinstance(m, dict):
        m["content"] = value
    else:
        setattr(m, "content", value)


def _role_of(m: Any) -> Any:
    return m.get("role") if isinstance(m, dict) else getattr(m, "role", None)
