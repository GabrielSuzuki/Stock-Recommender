"""Difficulty-based model routing.

The single biggest lever on cost: most requests in a real workload are
classification, extraction, formatting or short factual answers that a small
model handles perfectly. Routing those to Haiku instead of Opus is a ~5x
input / ~5x output saving on that slice of traffic.

Two modes:
  - "heuristic" (default): free, offline, ~0 latency. Scores the request on
    signals that correlate with needed capability.
  - "classifier": spends one very short Haiku call to grade difficulty 1-3.
    Costs ~$0.0002; worth it when the heuristic is too blunt for your traffic.

Both support an escalation safety net: if the cheap model's answer fails a
caller-supplied validator, the request is retried on the strong model. You
still win as long as the escalation rate is below ~20%.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from . import tokens

CHEAP = "cheap"
MID = "mid"
STRONG = "strong"

DEFAULT_TIERS = {
    CHEAP: "claude-haiku-4-5",
    MID: "claude-sonnet-5",
    STRONG: "claude-opus-5",
}

# Signals that the task is mechanical: transform text in, text out.
_SIMPLE = re.compile(
    r"\b(summari[sz]e|tl;?dr|extract|classif|categor|label|tag|translate|"
    r"rewrite|rephrase|proofread|spell|grammar|format|reformat|convert|"
    r"list|bullet|title|headline|subject line|sentiment|yes or no|"
    r"true or false|parse|normali[sz]e|dedup|sort|count)\b",
    re.I,
)

# Signals that the task needs real reasoning or long-horizon planning.
_HARD = re.compile(
    r"\b(prove|derive|architect|design (?:a|the|an) (?:system|schema|api)|"
    r"refactor|debug|root cause|why does|trade-?offs?|strategy|"
    r"step[- ]by[- ]step|reason through|optimi[sz]e|algorithm|complexity|"
    r"security review|threat model|migrat|concurren|race condition|"
    r"write (?:a|the) (?:program|service|library|compiler|parser))\b",
    re.I,
)

_CODE_FENCE = re.compile(r"```|\bdef \w+\(|\bclass \w+[:(]|=>|\bfunction \w+\(")

_CLASSIFIER_PROMPT = (
    "Grade how much model capability this request needs.\n"
    "1 = mechanical text transformation (extract, classify, reformat, short factual lookup).\n"
    "2 = moderate: multi-step but routine writing, straightforward code, analysis of given text.\n"
    "3 = hard: novel reasoning, system design, tricky debugging, math proofs, long-horizon planning.\n"
    "Answer with the single digit only."
)


@dataclass
class RouteDecision:
    tier: str
    model: str
    score: float
    reasons: list[str] = field(default_factory=list)
    baseline_model: str = ""
    classifier_cost: float = 0.0

    @property
    def downgraded(self) -> bool:
        return bool(self.baseline_model) and self.model != self.baseline_model


class Router:
    def __init__(
        self,
        tiers: Optional[dict[str, str]] = None,
        mode: str = "heuristic",
        baseline: Optional[str] = None,
        min_tier: str = CHEAP,
        classifier_model: Optional[str] = None,
    ) -> None:
        self.tiers = {**DEFAULT_TIERS, **(tiers or {})}
        if mode not in ("heuristic", "classifier", "off"):
            raise ValueError(f"unknown routing mode: {mode}")
        self.mode = mode
        self.baseline = baseline or self.tiers[STRONG]
        self.min_tier = min_tier
        self.classifier_model = classifier_model or self.tiers[CHEAP]

    # -- scoring -----------------------------------------------------------
    def score(self, messages: list[Any], system: Any = None, **params: Any) -> tuple[float, list[str]]:
        """Return a 0-1 difficulty score plus the reasons that moved it."""
        reasons: list[str] = []
        s = 0.35

        text = _flatten(messages[-3:]) if messages else ""
        full_tokens = tokens.estimate_messages(messages) + tokens.estimate_system(system)
        hard = bool(_HARD.search(text))

        if _SIMPLE.search(text) and not hard:
            s -= 0.22
            reasons.append("mechanical-verb")
        if hard:
            s += 0.28
            reasons.append("reasoning-verb")
        if _CODE_FENCE.search(text):
            s += 0.12
            reasons.append("code-present")
        # Brevity is only evidence of easiness when nothing else says otherwise:
        # "prove P != NP" is nine words and not a Haiku job.
        if not hard and "?" in text and len(text) < 200:
            s -= 0.08
            reasons.append("short-question")

        if full_tokens > 60_000:
            s += 0.18
            reasons.append("very-long-context")
        elif full_tokens > 12_000:
            s += 0.08
            reasons.append("long-context")
        elif full_tokens < 400 and not hard:
            s -= 0.10
            reasons.append("tiny-context")

        turns = len(messages)
        if turns >= 8:
            s += 0.10
            reasons.append("deep-conversation")

        n_tools = len(params.get("tools") or [])
        if n_tools >= 6:
            s += 0.15
            reasons.append("many-tools")
        elif n_tools:
            s += 0.06
            reasons.append("tools-present")

        if params.get("thinking"):
            # Asking for extended thinking is an explicit statement that the
            # task needs capability. Never quietly override that.
            reasons.append("thinking-requested")
            return 1.0, reasons

        max_tokens = params.get("max_tokens") or 0
        if max_tokens >= 8000:
            s += 0.12
            reasons.append("long-output-requested")
        elif 0 < max_tokens <= 256:
            s -= 0.10
            reasons.append("short-output-requested")

        return max(0.0, min(1.0, s)), reasons

    # -- routing -----------------------------------------------------------
    def route(
        self,
        messages: list[Any],
        system: Any = None,
        classify: Optional[Callable[[str], int]] = None,
        **params: Any,
    ) -> RouteDecision:
        baseline = params.get("model") or self.baseline

        if self.mode == "off":
            return RouteDecision(STRONG, baseline, 1.0, ["routing-off"], baseline)

        # Explicit per-call override always wins.
        forced = params.get("tier")
        if forced in self.tiers:
            return RouteDecision(forced, self.tiers[forced], 0.5, ["caller-forced"], baseline)

        s, reasons = self.score(messages, system, **params)
        cost = 0.0

        if self.mode == "classifier" and classify is not None:
            try:
                grade = classify(_flatten(messages[-2:]))
                s = 0.15 + 0.35 * (grade - 1)  # 1 -> .15, 2 -> .50, 3 -> .85
                reasons.append(f"classifier-grade-{grade}")
            except Exception as exc:  # never let the classifier break a request
                reasons.append(f"classifier-failed({type(exc).__name__})")

        if s < 0.30:
            tier = CHEAP
        elif s < 0.62:
            tier = MID
        else:
            tier = STRONG

        tier = _at_least(tier, self.min_tier)
        return RouteDecision(tier, self.tiers[tier], s, reasons, baseline, cost)

    def escalate(self, decision: RouteDecision) -> Optional[str]:
        """Next model up from a decision, or None if already at the top."""
        order = [CHEAP, MID, STRONG]
        i = order.index(decision.tier)
        return self.tiers[order[i + 1]] if i + 1 < len(order) else None


_ORDER = {CHEAP: 0, MID: 1, STRONG: 2}


def _at_least(tier: str, floor: str) -> str:
    return tier if _ORDER[tier] >= _ORDER[floor] else floor


def _flatten(messages: list[Any]) -> str:
    out: list[str] = []
    for m in messages:
        content = m.get("content") if isinstance(m, dict) else getattr(m, "content", m)
        if isinstance(content, str):
            out.append(content)
        elif isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "text":
                    out.append(b.get("text", ""))
                elif isinstance(b, str):
                    out.append(b)
    return "\n".join(out)


CLASSIFIER_PROMPT = _CLASSIFIER_PROMPT
