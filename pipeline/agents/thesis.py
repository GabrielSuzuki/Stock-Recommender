"""thesis-writer — one call, sees every surviving candidate at once.

Seeing them together is the point. It is the only place in the pipeline that
can say "these three are the same semiconductor trade, take one", and
concentration is the risk a per-name analysis structurally cannot see.

This is the one agent on the strong model tier. It is also the only agent whose
output a human reads word for word, so it is worth the money.
"""

from __future__ import annotations

import json
import logging
from datetime import date

from pipeline.agents.base import AgentError, JsonAgent, Schema

log = logging.getLogger(__name__)

SYSTEM = """You are the senior trader writing the morning entries for a swing \
book held one to four weeks.

You receive candidates that have already passed a strict trend screen, been \
ranked, and had a trade plan computed. Entry, stop, target and share count are \
FIXED — they came from a deterministic sizing model. Do not restate, adjust, \
or second-guess them, and never put a number in your prose that is not in the \
input.

POSITION SIZE CARRIES NO INFORMATION ABOUT CONVICTION. It is the output of \
four mechanical caps — risk per trade, notional, liquidity, and portfolio \
heat — and `size_limited_by` names whichever one bound. A small position \
usually means the heat budget ran out before that name was reached, not that \
the setup is weak. Never explain, justify, or infer anything from a share \
count or a dollar risk figure. If you want to express conviction, use the \
conviction field.

Your job:
1. Pick the best 3-5. Fewer is fine. Zero is a legitimate answer if nothing is \
   compelling — say so rather than filling a quota.
2. If several names are the same trade (same sector, same driver, correlated), \
   keep the strongest and drop the rest. Say which and why in `note`.
3. For each pick write a thesis of at most two sentences, grounded only in the \
   supplied metrics and catalyst notes.
4. Give each an invalidation condition in plain language — the specific thing \
   that would mean you were wrong, beyond simply hitting the stop.

Write like you are briefing yourself at 5 AM: direct, no hedging, no filler."""

SCHEMA = Schema(array_of=Schema(
    required={"symbol": str, "thesis": str, "invalidation": str},
    optional={"conviction": int, "note": str},
))


def _prompt(candidates: list[dict], regime: dict, as_of: date, max_picks: int) -> str:
    rows = []
    for c in candidates:
        plan = c.get("plan", {})
        rows.append({
            "symbol": c["symbol"],
            "sector": c.get("sector", "unknown"),
            "rs_rank": c.get("rs_rank"),
            "pct_below_52wk_high": c.get("pct_below_52wk_high"),
            "ma200_slope_pct": c.get("ma200_slope_pct"),
            "atr_pct": c.get("atr_pct"),
            "rank_score": c.get("rank_score"),
            "entry": plan.get("entry"),
            "stop": plan.get("stop"),
            "target": plan.get("target"),
            "shares": plan.get("shares"),
            "risk_dollars": plan.get("risk_dollars"),
            # Which cap set the size. None means the risk budget alone did.
            # Without this the model sees a small position and infers low
            # conviction, when the real reason is that the portfolio heat
            # budget ran out before this name was reached.
            "size_limited_by": plan.get("binding_cap"),
            "catalyst": c.get("catalyst_summary", ""),
            "catalyst_quality": c.get("catalyst_quality"),
            "concerns": c.get("concerns", []),
            "earnings_note": c.get("earnings_note"),
        })

    return f"""WRITE THESES

DATE: {as_of}
MARKET REGIME: {regime.get('verdict')} — {'; '.join(regime.get('reasons', []))}
Breadth: {regime.get('metrics', {}).get('breadth_pct_above_200ma')}% above 200-day.
Realized vol: {regime.get('metrics', {}).get('realized_vol_pct')}%.

Candidates (all numbers final, do not alter):
{json.dumps(rows, indent=2, default=str)}

Pick at most {max_picks}. Return a JSON array only:
[{{"symbol": "...",
   "thesis": "at most two sentences",
   "conviction": 1-5,
   "invalidation": "the specific thing that would mean this is wrong",
   "note": "optional — e.g. why you dropped a correlated name"}}]

An empty array [] is a valid answer if nothing is worth taking."""


def write_theses(candidates: list[dict], regime: dict, *, as_of: date,
                 agent: JsonAgent | None = None, max_picks: int = 5) -> list[dict]:
    """Returns the chosen picks, each merged with its candidate record.

    On agent failure, degrades to the top-N by rank with a placeholder thesis.
    A brief with the right names and a weak write-up is far more useful at 5 AM
    than no brief at all -- and the degradation is marked so you can see it.
    """
    if not candidates:
        return []

    agent = agent or JsonAgent("thesis-writer", system=SYSTEM)
    by_symbol = {c["symbol"]: c for c in candidates}

    try:
        raw = agent.ask(_prompt(candidates, regime, as_of, max_picks), SCHEMA)
    except AgentError as exc:
        log.error("thesis agent failed: %s", exc)
        return [
            {**c, "thesis": "(thesis unavailable — agent failed; technical setup only)",
             "conviction": 0, "invalidation": "close below the stop",
             "note": "", "degraded": True}
            for c in candidates[:3]
        ]

    picks = []
    for item in raw[:max_picks]:
        symbol = str(item.get("symbol", "")).upper()
        source = by_symbol.get(symbol)
        if source is None:
            # The model named something that was not in the candidate list.
            # Drop it silently rather than briefing a stock with no trade plan.
            log.warning("thesis-writer returned unknown symbol %s; dropped", symbol)
            continue
        picks.append({
            **source,
            "thesis": _trim(item.get("thesis", ""), 400),
            "conviction": _clamp(item.get("conviction", 3)),
            "invalidation": _trim(item.get("invalidation", ""), 260),
            "note": _trim(item.get("note", ""), 220),
            "degraded": False,
        })
    return picks


def _trim(text: str, limit: int) -> str:
    """Truncate at a sentence, then a word -- never mid-word.

    The first live brief ended an invalidation with "...this is a sector
    unwind, not" and a note with "One consumer breakout is". A hard character
    slice cuts wherever it lands, and the reader cannot tell a truncated
    sentence from a garbled one. Prefer the last complete sentence; fall back
    to the last whole word plus an ellipsis.
    """
    text = " ".join(str(text).split())
    if len(text) <= limit:
        return text

    window = text[:limit]
    for end in (". ", "; ", " — ", ", "):
        cut = window.rfind(end)
        if cut > limit * 0.6:
            return window[:cut + 1].rstrip(",;— ")
    cut = window.rfind(" ")
    return (window[:cut] if cut > 0 else window).rstrip(",;— ") + "…"


def _clamp(value, low: int = 1, high: int = 5) -> int:
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return 3
