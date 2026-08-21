"""catalyst-analyst — one call per candidate, run in parallel.

Reads the news, earnings date and filings for a single name and answers: is
there a reason a chart that looks perfect is actually uninvestable?

The hard disqualifications are decided in `mcp/news/disqualify.py`, before this
agent is called, and they cannot be overturned here. This agent adds the softer
read: what the news actually says, whether there is a real catalyst, and any
concern the regexes could not see.
"""

from __future__ import annotations

import concurrent.futures as futures
import logging
from datetime import date

from mcp.news import check_disqualifiers
from mcp.news.disqualify import upcoming_earnings_note
from pipeline.agents.base import AgentError, JsonAgent, Schema

log = logging.getLogger(__name__)

SYSTEM = """You are a catalyst analyst on a swing-trading desk. You are given \
one stock that has already passed a technical screen, plus its recent news, \
earnings date and SEC filings.

Your job is narrow: say what the news means for a 1-4 week hold.

Rules:
- Judge only from the material provided. If it is thin, say so; never fill the \
gap from memory. You do not know today's price or anything not in this prompt.
- Never restate or recompute a number. The numbers are already correct.
- Be short. Two sentences is usually right.
- Prefer "no clear catalyst" over inventing one. Most stocks most days have no \
catalyst, and saying so is useful."""

SCHEMA = Schema(
    required={"symbol": str, "catalyst_summary": str,
              "catalyst_quality": int, "disqualify": bool},
    optional={"concerns": list, "note": str},
)


def _prompt(symbol: str, metrics: dict, context, disq, as_of: date) -> str:
    headlines = context.headlines[:8]
    news_block = "\n".join(
        f"  - [{h.get('datetime', '?')}] {h.get('headline', '')}"
        f"{(' — ' + h['summary'][:160]) if h.get('summary') else ''}"
        for h in headlines) or "  (no news in the lookback window)"

    filings_block = "\n".join(
        f"  - {f.get('form')} filed {f.get('filed')} {f.get('description', '')}".rstrip()
        for f in context.filings[:8]) or "  (no watched filings)"

    earnings = context.earnings_date.isoformat() if context.earnings_date else "unknown"
    disq_block = ("  ALREADY DISQUALIFIED by mechanical rules: " + disq.summary
                  if disq.disqualified else "  (no mechanical disqualification)")

    return f"""CATALYST CHECK

SYMBOL: {symbol}
DATE: {as_of}

Technical context (already computed — do not recompute):
  RS rank {metrics.get('rs_rank')}
  {metrics.get('pct_below_52wk_high')}% below the 52-week high
  200-day slope {metrics.get('ma200_slope_pct')}%
  ATR {metrics.get('atr_pct')}% of price
  sector: {metrics.get('sector', 'unknown')}

Next earnings: {earnings}
{disq_block}

Recent news:
{news_block}

Recent filings:
{filings_block}

Return JSON only:
{{"symbol": "{symbol}",
  "catalyst_summary": "one or two sentences on what the news means for a 1-4 week hold",
  "catalyst_quality": 1-5 (1 = no catalyst / noise, 5 = strong specific catalyst),
  "concerns": ["short strings, empty list if none"],
  "disqualify": true only if the news shows something that makes this untradeable
                and the mechanical rules missed it}}"""


def analyse_one(symbol: str, metrics: dict, context, *, as_of: date,
                agent: JsonAgent | None = None, hold_days: int = 21) -> dict:
    """Catalyst read for one symbol. Never raises."""
    disq = check_disqualifiers(context, as_of=as_of, hold_days=hold_days)

    result = {
        "symbol": symbol,
        "disqualified": disq.disqualified,
        "disqualify_reason": disq.summary,
        "disqualify_detail": disq.detail,
        "earnings_note": upcoming_earnings_note(context, as_of),
        "news_count": len(context.headlines),
        "news_degraded": context.degraded,
        "catalyst_summary": "",
        "catalyst_quality": 0,
        "concerns": [],
        "agent_error": None,
    }

    # A mechanically disqualified name is dropped before the model sees it.
    # That is ~20-30% of a typical day's candidates and it is the cheapest
    # token saving in the pipeline -- there is nothing to reason about once
    # the answer is already no.
    if disq.disqualified:
        result["catalyst_summary"] = f"Disqualified: {disq.summary}"
        return result

    agent = agent or JsonAgent("catalyst-analyst", system=SYSTEM)
    try:
        raw = agent.ask(_prompt(symbol, metrics, context, disq, as_of), SCHEMA)
        from pipeline.agents.thesis import _trim
        result["catalyst_summary"] = _trim(raw.get("catalyst_summary", ""), 400)
        result["catalyst_quality"] = _clamp(raw.get("catalyst_quality", 0))
        result["concerns"] = [_trim(c, 120) for c in (raw.get("concerns") or [])][:4]
        if bool(raw.get("disqualify")):
            result["disqualified"] = True
            result["disqualify_reason"] = "analyst flagged: " + \
                (result["concerns"][0] if result["concerns"] else "see summary")
    except AgentError as exc:
        # Degraded, not fatal. A name with no catalyst read is still a valid
        # technical setup; the brief will simply say the read is missing.
        log.warning("catalyst agent failed for %s: %s", symbol, exc)
        result["agent_error"] = str(exc)
        result["catalyst_summary"] = "(catalyst read unavailable)"

    return result


def analyse_catalysts(candidates: list[dict], contexts: dict, *, as_of: date,
                      agent: JsonAgent | None = None, hold_days: int = 21,
                      max_workers: int = 6) -> list[dict]:
    """Fan out across candidates. Order of the input list is preserved."""
    from mcp.news import NewsContext

    def work(candidate: dict) -> dict:
        symbol = candidate["symbol"]
        ctx = contexts.get(symbol) or NewsContext(symbol=symbol,
                                                  errors=["no context fetched"])
        return analyse_one(symbol, candidate, ctx, as_of=as_of,
                           agent=agent, hold_days=hold_days)

    if not candidates:
        return []
    if max_workers <= 1:
        return [work(c) for c in candidates]

    with futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        return list(pool.map(work, candidates))


def _clamp(value, low: int = 0, high: int = 5) -> int:
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return 0
