"""Agent harness: get structured JSON out of a language model, reliably.

Three problems this solves, all of which will otherwise bite at 05:00:

  1. **Models emit prose around JSON.** Fenced blocks, a preamble, a trailing
     "Let me know if you'd like changes". `extract_json` handles all of it.
  2. **Schemas drift.** A missing key surfaces as a KeyError three functions
     later. `validate` checks the shape at the boundary and asks for a repair.
  3. **A failed agent must not kill the brief.** Every agent has a documented
     degraded mode, and the orchestrator prefers a thinner brief to no brief.

The model never computes numbers here -- prompts pass pre-computed metrics and
ask for judgement and prose. Anything numeric in the output is either copied
from the input or ignored downstream.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable

log = logging.getLogger(__name__)

FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


class AgentError(RuntimeError):
    """The agent could not produce usable output after a repair attempt."""


# --------------------------------------------------------------------------- #
# JSON extraction
# --------------------------------------------------------------------------- #

def extract_json(text: str) -> Any:
    """Pull the first JSON value out of a model response.

    Tries, in order: the whole string, a fenced code block, then the widest
    balanced {...} or [...] span. Raises ValueError if none of it parses.
    """
    if text is None:
        raise ValueError("empty response")
    text = text.strip()
    if not text:
        raise ValueError("empty response")

    for candidate in _candidates(text):
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    raise ValueError(f"no JSON found in response: {text[:200]!r}")


_DECODER = json.JSONDecoder()


def _candidates(text: str):
    """Yield progressively looser slices that might be the JSON payload.

    The last step scans for a complete JSON value starting at EVERY `{` or `[`,
    using the stdlib decoder's `raw_decode`. The obvious cheap heuristic --
    first `{` to last `}` -- breaks the moment the model writes prose
    containing braces before the payload ("note {x}\n{...}"), which produces a
    span that is not valid JSON at all. raw_decode also handles braces inside
    string literals and escapes correctly, which hand-rolled brace counting
    does not.
    """
    yield text
    for match in FENCE.finditer(text):
        yield match.group(1).strip()
    for i, ch in enumerate(text):
        if ch in "{[":
            try:
                _, end = _DECODER.raw_decode(text, i)
            except ValueError:
                continue
            yield text[i:end]


# --------------------------------------------------------------------------- #
# schema validation
# --------------------------------------------------------------------------- #

@dataclass
class Schema:
    """A deliberately small schema language.

    Full JSON Schema would be more expressive and much harder to read in a
    repair prompt. What matters here is that the description handed back to
    the model when validation fails is short and unambiguous.
    """
    required: dict[str, type | tuple[type, ...]] = field(default_factory=dict)
    optional: dict[str, type | tuple[type, ...]] = field(default_factory=dict)
    array_of: "Schema | None" = None

    def describe(self) -> str:
        if self.array_of is not None:
            return f"[{self.array_of.describe()}, ...]"
        parts = [f'"{k}": {_type_name(v)}' for k, v in self.required.items()]
        for k, v in self.optional.items():
            parts.append(f'"{k}": {_type_name(v)} (optional)')
        return "{" + ", ".join(parts) + "}"

    def validate(self, value: Any, path: str = "$") -> list[str]:
        problems: list[str] = []
        if self.array_of is not None:
            if not isinstance(value, list):
                return [f"{path}: expected a list"]
            for i, item in enumerate(value):
                problems += self.array_of.validate(item, f"{path}[{i}]")
            return problems

        if not isinstance(value, dict):
            return [f"{path}: expected an object"]
        for key, expected in self.required.items():
            if key not in value:
                problems.append(f"{path}.{key}: missing")
            elif not isinstance(value[key], expected):
                problems.append(
                    f"{path}.{key}: expected {_type_name(expected)}, "
                    f"got {type(value[key]).__name__}")
        for key, expected in self.optional.items():
            if key in value and value[key] is not None and not isinstance(value[key], expected):
                problems.append(f"{path}.{key}: expected {_type_name(expected)}")
        return problems


def _type_name(t) -> str:
    if isinstance(t, tuple):
        return "|".join(x.__name__ for x in t)
    return {str: "string", int: "number", float: "number", bool: "boolean",
            list: "array", dict: "object"}.get(t, t.__name__)


# --------------------------------------------------------------------------- #
# the agent
# --------------------------------------------------------------------------- #

class JsonAgent:
    """Wraps a tokenwise-managed client and enforces a JSON contract."""

    def __init__(self, role: str, *, client=None, system: str = "",
                 max_repairs: int = 1):
        self.role = role
        self.system = system
        self.max_repairs = max_repairs
        self._client = client

    @property
    def client(self):
        if self._client is None:
            from pipeline.llm import agent_for
            self._client = agent_for(self.role)
        return self._client

    def ask(self, prompt: str, schema: Schema | None = None) -> Any:
        """Send `prompt`, parse JSON, validate, and repair once if needed."""
        response = self._call(prompt)
        for attempt in range(self.max_repairs + 1):
            try:
                value = extract_json(response)
            except ValueError as exc:
                if attempt >= self.max_repairs:
                    raise AgentError(f"{self.role}: {exc}") from exc
                log.warning("%s: unparseable response, asking for repair", self.role)
                response = self._call(_repair_prompt(prompt, response, str(exc), schema))
                continue

            problems = schema.validate(value) if schema else []
            if not problems:
                return value
            if attempt >= self.max_repairs:
                raise AgentError(f"{self.role}: schema violations {problems[:5]}")
            log.warning("%s: schema problems %s, asking for repair", self.role, problems[:3])
            response = self._call(
                _repair_prompt(prompt, response, "; ".join(problems[:5]), schema))

        raise AgentError(f"{self.role}: exhausted repair attempts")

    def _call(self, prompt: str) -> str:
        return self.client.ask(prompt, system=self.system or None)


def _repair_prompt(original: str, bad_response: str, problem: str,
                   schema: Schema | None) -> str:
    shape = f"\n\nRequired shape:\n{schema.describe()}" if schema else ""
    return (
        "Your previous response could not be used.\n\n"
        f"Problem: {problem}{shape}\n\n"
        "Respond with the corrected JSON and nothing else -- no prose, no code "
        "fences, no explanation.\n\n"
        f"Previous response:\n{bad_response[:1500]}"
    )


# --------------------------------------------------------------------------- #
# offline stub
# --------------------------------------------------------------------------- #

class OfflineModel:
    """A stand-in client so Stage B runs with no API key.

    Not a mock in the testing sense -- it produces plausible, deterministic,
    schema-valid output for each role so the whole orchestration, the Telegram
    formatting and the journal write can be exercised end to end. What it
    cannot exercise is whether the real model's judgement is any good.
    """

    def __init__(self, responder: Callable[[str], str] | None = None):
        self.responder = responder
        self.calls: list[str] = []

    def ask(self, prompt: str, system: str | None = None, **_) -> str:
        self.calls.append(prompt)
        if self.responder is not None:
            return self.responder(prompt)
        return json.dumps(self._canned(prompt))

    def summary(self, since=None) -> dict:
        return {"requests": len(self.calls), "actual_cost": 0.0,
                "baseline_cost": 0.0, "saved": 0.0, "saved_pct": 0.0,
                "offline": True}

    @staticmethod
    def _canned(prompt: str) -> Any:
        if "CATALYST CHECK" in prompt:
            symbol = _find_symbol(prompt)
            return {"symbol": symbol, "catalyst_summary":
                    "Recent coverage is constructive; no single dominant catalyst.",
                    "catalyst_quality": 3, "concerns": [], "disqualify": False}
        if "WRITE THESES" in prompt:
            symbols = _find_symbols(prompt)[:4]
            return [{"symbol": s,
                     "thesis": f"{s} is in a confirmed Stage-2 uptrend with sector "
                               "relative strength; entry on continuation.",
                     "conviction": 3,
                     "invalidation": "a daily close below the stop, or loss of the 50-day",
                     "note": ""} for s in symbols]
        if "COMPOSE BRIEF" in prompt:
            return {"line": "Offline run — synthetic data, no live market read."}
        return {}


def _find_symbol(prompt: str) -> str:
    match = re.search(r"SYMBOL:\s*([A-Z0-9.\-]+)", prompt)
    return match.group(1) if match else "UNKNOWN"


def _find_symbols(prompt: str) -> list[str]:
    """Symbols from the candidate JSON, excluding the "..." in the output
    template that the naive pattern would otherwise pick up."""
    found = re.findall(r'"symbol":\s*"([A-Z0-9.\-]{1,6})"', prompt)
    seen, out = set(), []
    for s in found:
        if s not in seen and any(ch.isalnum() for ch in s):
            seen.add(s)
            out.append(s)
    return out
