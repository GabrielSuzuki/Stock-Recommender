#!/usr/bin/env python3
"""journal-analyst — the Sunday review.

    python3 -m pipeline.weekly_review [--weeks 4] [--offline] [--dry-run]

This is the only component that can make the system better over time, and the
one everyone skips. It answers three questions a backtest structurally cannot:

  1. **What did the regime filter actually cost or save?** Only measurable
     because the journal records RISK_OFF days too. A review that only sees the
     days you traded cannot see the days you were kept out of.
  2. **Which screen conditions correlated with winners?** Joining briefs to
     fills is the only way to find out whether, say, high RS rank is doing any
     work in YOUR results rather than in a backtest's.
  3. **Where did the thesis diverge from what happened?** The part no metric
     catches, and the reason there is an LLM in this script at all.

Everything numeric is computed in Python and handed to the model as a table.
The model writes the reading, never the arithmetic.
"""

from __future__ import annotations

import argparse
import json
import logging
import statistics
import sys
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp.journal import Journal                                   # noqa: E402
from pipeline import paths                                        # noqa: E402
from pipeline.agents.base import AgentError, JsonAgent, OfflineModel, Schema  # noqa: E402

log = logging.getLogger("weekly_review")

SYSTEM = """You review a swing trader's week. You are given tables of already-\
computed statistics — recommendations, fills, outcomes, regime days.

Rules:
- Never compute or restate a number. Cite only figures present in the input.
- One week is a tiny sample. Say "too early to tell" when it is, and mean it. \
Do not manufacture a pattern from four trades.
- Be specific about what to change and what to leave alone. "Keep going" is a \
valid and often correct recommendation.
- Flag anything that looks like a process failure (recommendations ignored, \
fills far from the planned entry, stops not honoured) ahead of anything that \
looks like a strategy result."""

SCHEMA = Schema(
    required={"summary": str, "observations": list},
    optional={"suggested_change": str, "confidence": str},
)


# --------------------------------------------------------------------------- #
# deterministic analysis
# --------------------------------------------------------------------------- #

def analyse(briefs: list[dict], fills: list[dict]) -> dict:
    """Everything measurable, computed in Python."""
    recommended: dict[str, list[dict]] = defaultdict(list)
    regime_counter: Counter = Counter()
    picks_per_day: list[int] = []

    for brief in briefs:
        regime_counter[brief.get("regime", "?")] += 1
        picks = brief.get("picks", [])
        picks_per_day.append(len(picks))
        for pick in picks:
            recommended[pick["symbol"]].append({
                "as_of": brief.get("as_of"),
                "conviction": pick.get("conviction"),
                "plan": pick.get("plan", {}),
                "thesis": pick.get("thesis", ""),
            })

    buys = [f for f in fills if f.get("side") == "buy"]
    sells = [f for f in fills if f.get("side") == "sell"]

    acted, ignored = [], []
    for symbol, recs in recommended.items():
        if any(f["symbol"] == symbol for f in buys):
            acted.append(symbol)
        else:
            ignored.append(symbol)

    # Fills with no matching recommendation. Not a bug -- discretionary trades
    # are allowed -- but worth knowing, because they are invisible to every
    # other measurement in the system.
    off_plan = sorted({f["symbol"] for f in buys} - set(recommended))

    slippage = []
    for fill in buys:
        recs = recommended.get(fill["symbol"], [])
        entry = recs[0]["plan"].get("entry") if recs else None
        if entry:
            slippage.append((fill["price"] - entry) / entry * 100)

    closed = _match_round_trips(buys, sells)

    return {
        "briefs": len(briefs),
        "regime_days": dict(regime_counter),
        "risk_off_days": regime_counter.get("RISK_OFF", 0),
        "avg_picks_per_brief": round(statistics.mean(picks_per_day), 2)
        if picks_per_day else 0.0,
        "names_recommended": len(recommended),
        "names_acted_on": len(acted),
        "names_ignored": len(ignored),
        "action_rate_pct": round(len(acted) / len(recommended) * 100, 1)
        if recommended else None,
        "ignored_symbols": sorted(ignored)[:12],
        "off_plan_buys": off_plan,
        "buys": len(buys),
        "sells": len(sells),
        "avg_entry_slippage_pct": round(statistics.mean(slippage), 3) if slippage else None,
        "worst_entry_slippage_pct": round(max(slippage), 3) if slippage else None,
        "round_trips": closed,
        "closed_count": len(closed),
        "realised_pnl": round(sum(t["pnl"] for t in closed), 2) if closed else 0.0,
        "win_rate_pct": round(sum(1 for t in closed if t["pnl"] > 0) / len(closed) * 100, 1)
        if closed else None,
    }


def _match_round_trips(buys: list[dict], sells: list[dict]) -> list[dict]:
    """FIFO-match sells against buys, per symbol.

    Deliberately simple and deliberately conservative: partial and unmatched
    sells are skipped rather than guessed at. A review built on invented
    matches is worse than one built on fewer real ones.
    """
    lots: dict[str, list[dict]] = defaultdict(list)
    for fill in sorted(buys, key=lambda f: f.get("logged_at", "")):
        lots[fill["symbol"]].append(dict(fill))

    closed = []
    for sell in sorted(sells, key=lambda f: f.get("logged_at", "")):
        queue = lots.get(sell["symbol"], [])
        remaining = sell["quantity"]
        while remaining > 0 and queue:
            lot = queue[0]
            matched = min(remaining, lot["quantity"])
            closed.append({
                "symbol": sell["symbol"],
                "quantity": matched,
                "entry": lot["price"],
                "exit": sell["price"],
                "pnl": round(matched * (sell["price"] - lot["price"]), 2),
                "return_pct": round((sell["price"] / lot["price"] - 1) * 100, 2),
            })
            lot["quantity"] -= matched
            remaining -= matched
            if lot["quantity"] <= 0:
                queue.pop(0)
    return closed


def condition_correlation(briefs: list[dict], analysis: dict) -> dict:
    """Do recommended-and-taken names differ from recommended-and-ignored ones?

    With a few weeks of data this is descriptive, not inferential -- it is
    reported so the numbers accumulate, and it should not be acted on until
    there are dozens of observations. The review prompt says so explicitly.
    """
    taken = {t["symbol"] for t in analysis["round_trips"]}
    winners = {t["symbol"] for t in analysis["round_trips"] if t["pnl"] > 0}

    stats: dict[str, dict] = {}
    for field in ("conviction",):
        wins, losses = [], []
        for brief in briefs:
            for pick in brief.get("picks", []):
                if pick["symbol"] not in taken:
                    continue
                value = pick.get(field)
                if value is None:
                    continue
                (wins if pick["symbol"] in winners else losses).append(value)
        if wins or losses:
            stats[field] = {
                "winners_mean": round(statistics.mean(wins), 2) if wins else None,
                "losers_mean": round(statistics.mean(losses), 2) if losses else None,
                "n_winners": len(wins), "n_losers": len(losses),
            }
    return stats


# --------------------------------------------------------------------------- #

def _prompt(analysis: dict, conditions: dict, weeks: int) -> str:
    return f"""WEEKLY REVIEW

Period: the last {weeks} week(s).

Activity (all figures already computed — do not recompute):
{json.dumps(analysis, indent=2, default=str)}

Condition breakdown:
{json.dumps(conditions, indent=2, default=str)}

Return JSON only:
{{"summary": "2-3 sentences on the week",
  "observations": ["short strings — what stands out, process before performance"],
  "suggested_change": "one concrete change, or empty string if none is warranted",
  "confidence": "low | medium | high — how much the sample supports the above"}}"""


def render(analysis: dict, conditions: dict, reading: dict, weeks: int) -> str:
    lines = [f"{'=' * 64}", f"WEEKLY REVIEW — last {weeks} week(s)", "=" * 64,
             f"  briefs               {analysis['briefs']}",
             f"  regime days          {analysis['regime_days']}",
             f"  names recommended    {analysis['names_recommended']}",
             f"  acted on             {analysis['names_acted_on']} "
             f"({analysis['action_rate_pct']}%)" if analysis['action_rate_pct'] is not None
             else f"  acted on             {analysis['names_acted_on']}",
             f"  buys / sells         {analysis['buys']} / {analysis['sells']}",
             f"  round trips closed   {analysis['closed_count']}",
             f"  realised P&L         ${analysis['realised_pnl']:,.2f}",
             f"  win rate             {analysis['win_rate_pct']}%"
             if analysis['win_rate_pct'] is not None else "  win rate             n/a"]

    if analysis["avg_entry_slippage_pct"] is not None:
        lines.append(f"  entry slippage       avg {analysis['avg_entry_slippage_pct']}%  "
                     f"worst {analysis['worst_entry_slippage_pct']}%")
    if analysis["off_plan_buys"]:
        lines.append(f"  OFF-PLAN buys        {', '.join(analysis['off_plan_buys'])}")
    if analysis["ignored_symbols"]:
        lines.append(f"  recommended, skipped {', '.join(analysis['ignored_symbols'])}")

    lines += ["", "  " + "-" * 60, f"  {reading.get('summary', '')}", ""]
    for note in reading.get("observations", [])[:6]:
        lines.append(f"  • {note}")
    if reading.get("suggested_change"):
        lines += ["", f"  SUGGESTED: {reading['suggested_change']}"]
    lines.append(f"  confidence: {reading.get('confidence', 'unknown')}")
    return "\n".join(lines)


def run(weeks: int = 4, offline: bool = False, dry_run: bool = False) -> dict:
    paths.ensure_dirs()
    paths.load_dotenv()

    journal = Journal(paths.JOURNAL_DIR)
    since = date.today() - timedelta(weeks=weeks)
    briefs = journal.read_briefs(since=since)
    fills = journal.read_fills(since=since)
    log.info("reviewing %d briefs and %d fills since %s", len(briefs), len(fills), since)

    analysis = analyse(briefs, fills)
    conditions = condition_correlation(briefs, analysis)

    if not briefs:
        reading = {"summary": "No briefs in the period — the pipeline did not run.",
                   "observations": ["Check the timers before reading anything else "
                                    "into this."],
                   "confidence": "high"}
    else:
        agent = JsonAgent("journal-analyst", system=SYSTEM,
                          client=OfflineModel(_offline_responder) if offline else None)
        try:
            reading = agent.ask(_prompt(analysis, conditions, weeks), SCHEMA)
        except AgentError as exc:
            log.warning("review agent failed: %s", exc)
            reading = {"summary": "(agent unavailable — figures only)",
                       "observations": [], "confidence": "unknown"}

    text = render(analysis, conditions, reading, weeks)
    print(text)

    if not dry_run:
        try:
            from notify.telegram import Telegram
            Telegram().send(text, parse_mode="")
        except Exception as exc:                        # noqa: BLE001
            log.error("could not send the review: %s", exc)

    record = {"as_of": str(date.today()), "weeks": weeks,
              "analysis": analysis, "conditions": conditions, "reading": reading}
    (paths.JOURNAL_DIR / "reviews.jsonl").open("a").write(
        json.dumps(record, default=str) + "\n")
    return record


def _offline_responder(prompt: str) -> str:
    return json.dumps({
        "summary": "Offline review — synthetic reading, no model was called.",
        "observations": ["Sample is too small to conclude anything."],
        "suggested_change": "",
        "confidence": "low"})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Weekly journal review")
    parser.add_argument("--weeks", type=int, default=4)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")
    try:
        run(weeks=args.weeks, offline=args.offline, dry_run=args.dry_run)
        return 0
    except Exception as exc:                            # noqa: BLE001
        log.error("weekly review failed: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
