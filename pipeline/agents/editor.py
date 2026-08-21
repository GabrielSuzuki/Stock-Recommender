"""brief-editor — renders the Telegram message.

DESIGN NOTE, a second deliberate departure from architecture.md v1.1:

    The brief is RENDERED DETERMINISTICALLY from the structured picks. The
    model writes one sentence -- the market line -- and nothing else.

    The original design had an agent "compress everything into a
    Telegram-shaped message". That hands a model three jobs it is bad at and
    that fail silently: producing valid MarkdownV2 (one unescaped `.` and
    Telegram rejects the whole message), staying under 4096 characters, and
    never altering a number on the way through. Each is a formatting bug that
    only shows up at 05:00, and the third is a correctness bug you might not
    notice at all.

    thesis-writer already caps each thesis at two sentences, so there was very
    little compression left to do. What remained was risk. So the layout is
    code, escaping is `escape_md`, length is enforced by construction, and
    every number is interpolated straight from candidates.json.

    The model still writes the one thing it is genuinely better at: the
    sentence that says what kind of morning this is.
"""

from __future__ import annotations

import logging
from datetime import date

from notify.telegram import escape_md
from pipeline.agents.base import AgentError, JsonAgent, Schema

log = logging.getLogger(__name__)

SYSTEM = """You write the single opening line of a trader's morning brief.

One sentence. Plain, specific, no hedging, no exclamation marks. State what \
kind of tape it is and what that means for taking new entries today. Use only \
the figures given. Under 140 characters."""

SCHEMA = Schema(required={"line": str})

REGIME_ICON = {"RISK_ON": "\U0001F7E2", "NEUTRAL": "\U0001F7E1", "RISK_OFF": "\U0001F534"}

MAX_MESSAGE = 3800          # Telegram's cap is 4096; leave room for the footer


def market_line(regime: dict, candidate_count: int, *,
                agent: JsonAgent | None = None) -> str:
    """One LLM sentence about the tape. Falls back to the rule text."""
    fallback = "; ".join(regime.get("reasons", [])) or "no regime signal"
    metrics = regime.get("metrics", {})
    bench = metrics.get("benchmark") or {}

    agent = agent or JsonAgent("brief-editor", system=SYSTEM)
    prompt = f"""COMPOSE BRIEF — market line

Regime: {regime.get('verdict')}
Rules triggered: {fallback}
Benchmark {bench.get('symbol', 'SPY')} {bench.get('close')}, \
5-day change {bench.get('change_5d_pct')}%
Above 50-day: {bench.get('above_ma50')}   Above 200-day: {bench.get('above_ma200')}
Breadth above 200-day: {metrics.get('breadth_pct_above_200ma')}%
Realized vol: {metrics.get('realized_vol_pct')}%
Candidates passing the screen today: {candidate_count}

Return JSON only: {{"line": "one sentence, under 140 characters"}}"""

    try:
        return str(agent.ask(prompt, SCHEMA)["line"]).strip()[:180] or fallback
    except (AgentError, KeyError, TypeError) as exc:
        log.warning("market line failed, using rule text: %s", exc)
        return fallback


def render(picks: list[dict], regime: dict, summary: dict, as_of: date,
           line: str, *, cost_note: str = "") -> str:
    """Build the MarkdownV2 message. Pure function -- unit-testable, no model."""
    e = escape_md
    icon = REGIME_ICON.get(regime.get("verdict", ""), "")
    head = (f"*Morning Brief — {e(as_of.strftime('%a %d %b'))}*\n"
            f"{icon} *{e(regime.get('verdict', '?'))}* — {e(line)}")

    # Sell signals come FIRST, above the regime line's consequences and above
    # any new ideas. Managing what you already own outbids finding something
    # new, and on a RISK_OFF morning it is the only thing that matters.
    exits = _exit_block(summary)

    if regime.get("verdict") == "RISK_OFF":
        return (f"{head}{exits}\n\n_No new entries today\\._\n"
                f"{_bullets(regime.get('reasons', []))}"
                f"{_footer(summary, cost_note)}")

    exits = _exit_block(summary)

    if not picks:
        return (f"{head}{exits}\n\n_Nothing passed the screen today\\._\n"
                f"{_funnel_note(summary)}{_footer(summary, cost_note)}")

    blocks = [head + exits, ""]
    for p in picks:
        plan = p.get("plan", {})
        conviction = "★" * int(p.get("conviction") or 0)
        blocks.append(
            f"*{e(p['symbol'])}*  {e(conviction)}\n"
            f"entry {e(_num(plan.get('entry')))}  ·  stop {e(_num(plan.get('stop')))}  ·  "
            f"target {e(_num(plan.get('target')))}\n"
            f"{e(plan.get('shares') or 0)} sh  ·  risk {e(_money(plan.get('risk_dollars')))}\n"
            f"{e(p.get('thesis', ''))}\n"
            f"_invalid if_ {e(p.get('invalidation', 'stop is hit'))}"
            + (f"\n_{e(p['earnings_note'])}_" if p.get("earnings_note") else "")
            + (f"\n_note: {e(p['note'])}_" if p.get("note") else "")
        )
        blocks.append("")

    body = "\n".join(blocks) + _footer(summary, cost_note)
    if len(body) > MAX_MESSAGE:
        # Drop the weakest picks rather than let Telegram split mid-thesis.
        return render(picks[:-1], regime, summary, as_of, line, cost_note=cost_note)
    return body


def _exit_block(summary: dict) -> str:
    """Sell and trim signals for positions already held.

    Rendered from the same deterministic exit rules the position tracker uses,
    never from model output -- a hallucinated "sell" is the most expensive
    possible failure in this system.
    """
    signals = summary.get("exit_signals") or []
    urgent = [s for s in signals if s.get("action") in ("SELL", "TRIM")]
    watch = [s for s in signals if s.get("action") == "WATCH"]
    if not urgent and not watch:
        return ""

    lines = ["", "*MANAGE FIRST*"]
    for signal in urgent[:6]:
        icon = "\U0001F534" if signal["action"] == "SELL" else "\U0001F7E0"
        lines.append(f"{icon} *{escape_md(signal['action'])} "
                     f"{escape_md(signal['symbol'])}* — {escape_md(signal.get('detail', ''))}")
    if watch:
        names = ", ".join(s["symbol"] for s in watch[:6])
        lines.append(f"_watch:_ {escape_md(names)}")
    return "\n".join(lines)


def _footer(summary: dict, cost_note: str) -> str:
    parts = []
    heat = summary.get("portfolio_heat_pct")
    if heat is not None:
        parts.append(f"heat {heat:.1f}%")
    if summary.get("disqualified"):
        parts.append(f"{summary['disqualified']} disqualified")
    if summary.get("degraded"):
        parts.append("DEGRADED RUN")
    if cost_note:
        parts.append(cost_note)
    line = escape_md(" · ".join(parts)) if parts else ""
    reply = "\n_Reply_ `TOOK TICKER QTY @ PRICE` _to log a fill\\._"
    return (f"\n{line}{reply}" if line else reply)


def _funnel_note(summary: dict) -> str:
    killer = summary.get("top_eliminator")
    if not killer:
        return ""
    return (f"{escape_md(killer['label'])} eliminated "
            f"{escape_md(killer['eliminated'])} of "
            f"{escape_md(summary.get('evaluated', '?'))}\\.\n")


def _bullets(items: list[str]) -> str:
    return "".join(f"• {escape_md(i)}\n" for i in items[:4])


def _num(value) -> str:
    return "—" if value is None else f"{float(value):.2f}"


def _money(value) -> str:
    return "—" if value is None else f"${float(value):,.0f}"


def compose_brief(picks: list[dict], regime: dict, summary: dict, as_of: date,
                  *, agent: JsonAgent | None = None, cost_note: str = "") -> str:
    """Market line from the model, everything else from code."""
    line = market_line(regime, len(picks), agent=agent)
    return render(picks, regime, summary, as_of, line, cost_note=cost_note)
