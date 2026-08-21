"""Model pricing table and cost math.

Prices are USD per million tokens, verified against
https://platform.claude.com/docs/en/about-claude/pricing (August 2026).

Cache read is 0.1x base input; 5-minute cache write is 1.25x base input.
Override at runtime with `set_price()` or a JSON file via TOKENWISE_PRICING.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

MILLION = 1_000_000


@dataclass(frozen=True)
class Price:
    """Per-million-token prices for one model."""

    input: float
    output: float
    cache_write_5m: float
    cache_read: float

    @classmethod
    def from_input_output(cls, inp: float, out: float) -> "Price":
        return cls(input=inp, output=out, cache_write_5m=inp * 1.25, cache_read=inp * 0.1)


# --- The table ------------------------------------------------------------
PRICES: dict[str, Price] = {
    "claude-fable-5": Price.from_input_output(10.0, 50.0),
    "claude-mythos-5": Price.from_input_output(10.0, 50.0),
    "claude-opus-5": Price.from_input_output(5.0, 25.0),
    "claude-opus-4-8": Price.from_input_output(5.0, 25.0),
    "claude-opus-4-7": Price.from_input_output(5.0, 25.0),
    "claude-opus-4-6": Price.from_input_output(5.0, 25.0),
    "claude-opus-4-5": Price.from_input_output(5.0, 25.0),
    "claude-opus-4-1": Price.from_input_output(15.0, 75.0),
    "claude-opus-4": Price.from_input_output(15.0, 75.0),
    "claude-sonnet-5": Price.from_input_output(2.0, 10.0),
    "claude-sonnet-4-6": Price.from_input_output(3.0, 15.0),
    "claude-sonnet-4-5": Price.from_input_output(3.0, 15.0),
    "claude-sonnet-4": Price.from_input_output(3.0, 15.0),
    "claude-haiku-4-5": Price.from_input_output(1.0, 5.0),
    "claude-haiku-3-5": Price.from_input_output(0.80, 4.0),
}

# Fallback used when an unknown/dated model id shows up. Deliberately the
# priciest common tier so cost estimates never silently under-report.
_FALLBACK = PRICES["claude-opus-5"]


def _load_overrides() -> None:
    path = os.environ.get("TOKENWISE_PRICING")
    if not path or not os.path.exists(path):
        return
    with open(path) as fh:
        data = json.load(fh)
    for model, vals in data.items():
        PRICES[model] = Price(
            input=vals["input"],
            output=vals["output"],
            cache_write_5m=vals.get("cache_write_5m", vals["input"] * 1.25),
            cache_read=vals.get("cache_read", vals["input"] * 0.1),
        )


_load_overrides()


def set_price(model: str, price: Price) -> None:
    """Register or replace the price entry for a model."""
    PRICES[model] = price


def normalize(model: str) -> str:
    """Strip dated suffixes: 'claude-sonnet-4-5-20250929' -> 'claude-sonnet-4-5'."""
    if model in PRICES:
        return model
    parts = model.split("-")
    while parts:
        candidate = "-".join(parts)
        if candidate in PRICES:
            return candidate
        parts.pop()
    return model


def price_for(model: str) -> Price:
    return PRICES.get(normalize(model), _FALLBACK)


def cost(
    model: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_write_tokens: int = 0,
    cache_read_tokens: int = 0,
) -> float:
    """Dollar cost of one request's token counts.

    `input_tokens` should be the *uncached* input count, matching the
    Anthropic usage object where `input_tokens` excludes cache hits/writes.
    """
    p = price_for(model)
    return (
        input_tokens * p.input
        + output_tokens * p.output
        + cache_write_tokens * p.cache_write_5m
        + cache_read_tokens * p.cache_read
    ) / MILLION
