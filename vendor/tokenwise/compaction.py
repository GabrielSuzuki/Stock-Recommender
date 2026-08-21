"""Context compaction: stop paying to re-send junk on every turn.

In a long agent loop the input side dominates spend, because the whole
transcript is resent each turn. Most of that transcript is dead weight:
20k-token tool results the model already extracted one number from, the same
file read three times, thinking blocks from turns that are long over.

Passes, cheapest and safest first:
  1. strip stale thinking blocks
  2. middle-out truncate oversized tool results
  3. dedupe identical repeated payloads (keep first, point back to it)
  4. if still over budget, evict the middle of the conversation, optionally
     replacing it with a model-written summary

The message list is left API-valid: tool_use/tool_result pairing is repaired
after any eviction, so you never get a 400 from a dangling tool_result.
"""

from __future__ import annotations

import copy
import hashlib
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from . import tokens

ELIDED = "\n\n[... {n} tokens elided by tokenwise ...]\n\n"


@dataclass
class CompactionReport:
    before_tokens: int = 0
    after_tokens: int = 0
    actions: list[str] = field(default_factory=list)
    evicted_messages: int = 0
    summarized: bool = False

    @property
    def saved_tokens(self) -> int:
        return max(0, self.before_tokens - self.after_tokens)


class Compactor:
    def __init__(
        self,
        max_context_tokens: int = 120_000,
        tool_result_max_tokens: int = 2_000,
        keep_head: int = 2,
        keep_tail: int = 6,
        protect_recent: int = 2,
        dedupe: bool = True,
        drop_thinking: bool = True,
        summarizer: Optional[Callable[[list[Any]], str]] = None,
        enabled: bool = True,
    ) -> None:
        self.max_context_tokens = max_context_tokens
        self.tool_result_max_tokens = tool_result_max_tokens
        self.keep_head = keep_head
        self.keep_tail = keep_tail
        self.protect_recent = protect_recent
        self.dedupe = dedupe
        self.drop_thinking = drop_thinking
        self.summarizer = summarizer
        self.enabled = enabled

    # -- entry point -------------------------------------------------------
    def compact(self, messages: list[Any]) -> tuple[list[Any], CompactionReport]:
        report = CompactionReport(before_tokens=tokens.estimate_messages(messages))
        if not self.enabled or not messages:
            report.after_tokens = report.before_tokens
            return messages, report

        msgs = copy.deepcopy(messages)
        protected_from = max(0, len(msgs) - self.protect_recent)

        if self.drop_thinking:
            n = self._strip_thinking(msgs, protected_from)
            if n:
                report.actions.append(f"dropped {n} stale thinking block(s)")

        n, saved = self._truncate_tool_results(msgs, protected_from)
        if n:
            report.actions.append(f"truncated {n} oversized tool result(s) (~{saved} tokens)")

        if self.dedupe:
            n, saved = self._dedupe(msgs, protected_from)
            if n:
                report.actions.append(f"deduped {n} repeated payload(s) (~{saved} tokens)")

        current = tokens.estimate_messages(msgs)
        if current > self.max_context_tokens:
            msgs, evicted, summarized = self._evict_middle(msgs)
            report.evicted_messages = evicted
            report.summarized = summarized
            if evicted:
                report.actions.append(
                    f"evicted {evicted} middle message(s)"
                    + (" and replaced them with a summary" if summarized else "")
                )

        msgs = _repair_tool_pairs(msgs)
        msgs = [m for m in msgs if _content_of(m) not in (None, [], "")]
        report.after_tokens = tokens.estimate_messages(msgs)
        return msgs, report

    # -- passes ------------------------------------------------------------
    def _strip_thinking(self, msgs: list[Any], protected_from: int) -> int:
        removed = 0
        for i, m in enumerate(msgs):
            if i >= protected_from or _role_of(m) != "assistant":
                continue
            blocks = _content_of(m)
            if not isinstance(blocks, list):
                continue
            kept = [b for b in blocks if _btype(b) not in ("thinking", "redacted_thinking")]
            removed += len(blocks) - len(kept)
            _set_content(m, kept)
        return removed

    def _truncate_tool_results(self, msgs: list[Any], protected_from: int) -> tuple[int, int]:
        count = saved = 0
        limit = self.tool_result_max_tokens
        for i, m in enumerate(msgs):
            if i >= protected_from:
                continue
            blocks = _content_of(m)
            if not isinstance(blocks, list):
                continue
            for b in blocks:
                if not isinstance(b, dict) or b.get("type") != "tool_result":
                    continue
                text = _tool_result_text(b)
                if text is None:
                    continue
                est = tokens.estimate_text(text)
                if est <= limit:
                    continue
                b["content"] = _middle_out(text, limit)
                saved += est - tokens.estimate_text(b["content"])
                count += 1
        return count, saved

    def _dedupe(self, msgs: list[Any], protected_from: int) -> tuple[int, int]:
        seen: dict[str, tuple[int, int]] = {}
        count = saved = 0
        for i, m in enumerate(msgs):
            blocks = _content_of(m)
            if not isinstance(blocks, list):
                continue
            for j, b in enumerate(blocks):
                text = _payload_text(b)
                if text is None or len(text) < 500:
                    continue
                key = hashlib.sha1(text.encode("utf-8", "ignore")).hexdigest()
                if key not in seen:
                    seen[key] = (i, j)
                    continue
                if i >= protected_from:
                    continue  # never elide something the model is about to use
                first_i, _ = seen[key]
                est = tokens.estimate_text(text)
                note = (
                    f"[identical to the payload already shown in message {first_i}; "
                    f"{est} tokens elided by tokenwise]"
                )
                _replace_payload(b, note)
                saved += est - tokens.estimate_text(note)
                count += 1
        return count, saved

    def _evict_middle(self, msgs: list[Any]) -> tuple[list[Any], int, bool]:
        head, tail = self.keep_head, self.keep_tail
        if len(msgs) <= head + tail + 1:
            return msgs, 0, False

        middle = msgs[head : len(msgs) - tail]
        kept_head = msgs[:head]
        kept_tail = msgs[len(msgs) - tail :]

        # Shrink the middle from the front until we fit.
        budget = self.max_context_tokens - tokens.estimate_messages(kept_head + kept_tail)
        drop_until = 0
        while drop_until < len(middle) and tokens.estimate_messages(middle[drop_until:]) > budget:
            drop_until += 1
        dropped, retained = middle[:drop_until], middle[drop_until:]
        if not dropped:
            return msgs, 0, False

        summarized = False
        bridge: list[Any] = []
        if self.summarizer is not None:
            try:
                summary = self.summarizer(dropped)
                if summary:
                    bridge = [
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "text",
                                    "text": "[Summary of earlier conversation, compacted to save "
                                    f"context]\n{summary}",
                                }
                            ],
                        },
                        {"role": "assistant", "content": [{"type": "text", "text": "Understood."}]},
                    ]
                    summarized = True
            except Exception:
                bridge = []
        if not bridge:
            bridge = [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": f"[{len(dropped)} earlier messages were removed to save context. "
                            "Ask if you need details from them.]",
                        }
                    ],
                },
                {"role": "assistant", "content": [{"type": "text", "text": "Understood."}]},
            ]

        return kept_head + bridge + retained + kept_tail, len(dropped), summarized


# -- helpers ---------------------------------------------------------------
def _middle_out(text: str, limit_tokens: int) -> str:
    """Keep the head and tail of a payload -- the parts that carry structure."""
    keep_chars = max(200, int(limit_tokens * 3.4))
    head = int(keep_chars * 0.6)
    tail = keep_chars - head
    elided = tokens.estimate_text(text) - limit_tokens
    return text[:head] + ELIDED.format(n=elided) + text[-tail:]


def _tool_result_text(block: dict) -> Optional[str]:
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
        return "\n".join(parts) if parts else None
    return None


def _payload_text(block: Any) -> Optional[str]:
    if not isinstance(block, dict):
        return None
    if block.get("type") == "text":
        return block.get("text")
    if block.get("type") == "tool_result":
        return _tool_result_text(block)
    return None


def _replace_payload(block: dict, note: str) -> None:
    if block.get("type") == "text":
        block["text"] = note
    else:
        block["content"] = note


def _repair_tool_pairs(msgs: list[Any]) -> list[Any]:
    """Drop tool_results with no surviving tool_use, and vice versa."""
    use_ids = set()
    for m in msgs:
        for b in _blocks(m):
            if _btype(b) == "tool_use":
                use_ids.add(b.get("id"))
    result_ids = {
        b.get("tool_use_id") for m in msgs for b in _blocks(m) if _btype(b) == "tool_result"
    }

    for m in msgs:
        blocks = _content_of(m)
        if not isinstance(blocks, list):
            continue
        kept = []
        for b in blocks:
            t = _btype(b)
            if t == "tool_result" and b.get("tool_use_id") not in use_ids:
                continue
            if t == "tool_use" and b.get("id") not in result_ids:
                kept.append(
                    {
                        "type": "text",
                        "text": f"[called tool {b.get('name')}; result elided by tokenwise]",
                    }
                )
                continue
            kept.append(b)
        _set_content(m, kept)
    return msgs


def _blocks(m: Any) -> list:
    c = _content_of(m)
    return c if isinstance(c, list) else []


def _btype(b: Any) -> Optional[str]:
    return b.get("type") if isinstance(b, dict) else None


def _content_of(m: Any) -> Any:
    return m.get("content") if isinstance(m, dict) else getattr(m, "content", None)


def _set_content(m: Any, value: Any) -> None:
    if isinstance(m, dict):
        m["content"] = value
    else:
        setattr(m, "content", value)


def _role_of(m: Any) -> Any:
    return m.get("role") if isinstance(m, dict) else getattr(m, "role", None)
