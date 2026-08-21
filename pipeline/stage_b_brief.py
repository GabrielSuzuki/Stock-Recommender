#!/usr/bin/env python3
"""Stage B — 05:00 Pacific. Read candidates.json, decide, push to Telegram.

    python3 -m pipeline.stage_b_brief [--offline] [--dry-run]

    --offline   synthetic news and a stub model; no API keys needed
    --dry-run   do everything except send the Telegram message

Order of operations, and why:

    1. regime      computed in Python. If RISK_OFF, the brief goes out saying
                   "no entries" and nothing else runs. This is the cheapest and
                   most valuable step -- most of a swing book's drawdown comes
                   from taking good setups in bad tape.
    2. news        fetched only for the ~20 screened names, never the universe.
    3. disqualify  mechanical rules drop names before the model sees them.
    4. catalysts   one cheap model call per survivor, in parallel.
    5. theses      one strong model call over all survivors together.
    6. render      deterministic. The model writes one sentence.
    7. deliver     Telegram, then journal, then heartbeat.

A message goes out on every path, including failure. Silence must never be
ambiguous: if no message arrives, something is broken, and that is the only
thing silence is allowed to mean.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import traceback
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.regime import RISK_OFF, RegimeConfig, assess                # noqa: E402
from mcp.journal import Journal                                        # noqa: E402
from mcp.market_data import BarCache                                   # noqa: E402
from mcp.news import get_context, resolve_news_provider                # noqa: E402
from pipeline import paths                                             # noqa: E402
from pipeline.agents import analyse_catalysts, compose_brief, write_theses  # noqa: E402
from pipeline.agents.base import JsonAgent, OfflineModel               # noqa: E402

log = logging.getLogger("stage_b")

MAX_PICKS = 5
BENCHMARK_LOOKBACK = 260


class StageBError(RuntimeError):
    pass


def load_candidates(path: Path | None = None) -> dict:
    path = path or paths.CANDIDATES
    if not path.exists():
        raise StageBError(
            f"{path} does not exist — Stage A did not run or did not finish")
    payload = json.loads(path.read_text())

    as_of = date.fromisoformat(payload["run"]["as_of"])
    age = (date.today() - as_of).days
    if age > 4:
        # Refuse rather than brief on stale prices. A brief built from last
        # week's screen looks entirely normal and is entirely wrong.
        raise StageBError(f"candidates.json is for {as_of} ({age} days old)")
    return payload


def _agents(offline: bool) -> dict:
    if not offline:
        return {"catalyst": None, "thesis": None, "editor": None}
    stub = OfflineModel()
    return {
        "catalyst": JsonAgent("catalyst-analyst", client=stub),
        "thesis": JsonAgent("thesis-writer", client=stub),
        "editor": JsonAgent("brief-editor", client=stub),
    }


def run(offline: bool = False, dry_run: bool = False,
        candidates_path: Path | None = None) -> dict:
    started = time.monotonic()
    paths.ensure_dirs()
    paths.load_dotenv()

    payload = load_candidates(candidates_path)
    as_of = date.fromisoformat(payload["run"]["as_of"])
    candidates = payload.get("candidates", [])
    log.info("loaded %d candidates for %s", len(candidates), as_of)

    agents = _agents(offline)
    journal = Journal(paths.JOURNAL_DIR)
    notifier = _notifier(dry_run)

    # -- 1. regime ---------------------------------------------------------
    regime_cfg = RegimeConfig.from_yaml()
    reading = _assess_regime(regime_cfg)
    log.info("regime: %s — %s", reading.verdict, "; ".join(reading.reasons))

    # Exit signals for what is already held. Computed before the regime branch
    # so they still appear on a RISK_OFF morning -- standing down from new
    # entries has nothing to do with managing open risk.
    exit_signals = _exit_signals(offline)
    if exit_signals:
        log.info("exits: %d signal(s), %d urgent", len(exit_signals),
                 sum(1 for s in exit_signals if s["action"] in ("SELL", "TRIM")))

    summary = {
        "exit_signals": exit_signals,
        "portfolio_heat_pct": payload.get("portfolio", {}).get("portfolio_heat_pct"),
        "evaluated": payload.get("screen", {}).get("evaluated"),
        "top_eliminator": _top_eliminator(payload.get("funnel", [])),
        "degraded": reading.degraded,
        "disqualified": 0,
    }

    if reading.verdict == RISK_OFF:
        # Stop here. No news calls, no model calls beyond the one-line summary.
        # A risk-off morning should be the cheapest morning of the month.
        text = compose_brief([], reading.to_dict(), summary, as_of,
                             agent=agents["editor"])
        return _deliver(text, [], reading, summary, payload, journal, notifier,
                        as_of, started, offline, dry_run)

    if not candidates:
        text = compose_brief([], reading.to_dict(), summary, as_of,
                             agent=agents["editor"])
        return _deliver(text, [], reading, summary, payload, journal, notifier,
                        as_of, started, offline, dry_run)

    # -- 2/3/4. news, disqualify, catalysts --------------------------------
    symbols = [c["symbol"] for c in candidates]
    news_provider = resolve_news_provider("offline" if offline else None)
    edgar = None if offline else _edgar()
    contexts = get_context(symbols, as_of=as_of, provider=news_provider, edgar=edgar)

    analyses = analyse_catalysts(candidates, contexts, as_of=as_of,
                                 agent=agents["catalyst"])
    merged = _merge(candidates, analyses)

    survivors = [c for c in merged
                 if not c["disqualified"] and c.get("plan", {}).get("sizable")]
    summary["disqualified"] = sum(1 for c in merged if c["disqualified"])
    log.info("catalysts: %d disqualified, %d survivors",
             summary["disqualified"], len(survivors))

    # -- 5. theses ---------------------------------------------------------
    picks = write_theses(survivors, reading.to_dict(), as_of=as_of,
                         agent=agents["thesis"], max_picks=MAX_PICKS)
    log.info("theses: %d picks", len(picks))

    # -- 6. render ---------------------------------------------------------
    text = compose_brief(picks, reading.to_dict(), summary, as_of,
                         agent=agents["editor"], cost_note=_cost_note(offline))

    return _deliver(text, picks, reading, summary, payload, journal, notifier,
                    as_of, started, offline, dry_run)


# --------------------------------------------------------------------------- #

def _exit_signals(offline: bool) -> list[dict]:
    """Run the position tracker. Never raises -- a broken tracker must not stop
    the brief, but a silent one would be worse, so failures are logged loudly."""
    try:
        from pipeline.positions import (open_positions, plans_from_briefs,
                                        quotes_for)
        journal = Journal(paths.JOURNAL_DIR)
        plans = plans_from_briefs(journal)
        bootstrap = open_positions(journal, plans=plans)
        if not bootstrap:
            return []
        quotes = quotes_for([p.symbol for p in bootstrap], offline=offline)
        positions = open_positions(journal, plans=plans, quotes=quotes)
        from core.exits import ExitConfig, evaluate_book
        signals = evaluate_book(positions, quotes, as_of=date.today(),
                                config=ExitConfig.from_yaml())
        return [s.to_dict() for s in signals]
    except Exception as exc:                              # noqa: BLE001
        log.error("exit check failed: %s", exc)
        return []


def _assess_regime(cfg: RegimeConfig):
    """Regime from the cached bars. Never raises -- a regime we cannot compute
    degrades to NEUTRAL, which is the safe direction."""
    from core.regime import RegimeReading

    try:
        cache = BarCache(paths.BARS_DB)
        symbols = _cached_symbols(cache)
        bars = cache.read_many(symbols)
        bars = {s: b for s, b in bars.items() if not b.empty}
        bench = bars.get(cfg.benchmark)
        return assess(bars, benchmark_bars=bench, config=cfg)
    except Exception as exc:                              # noqa: BLE001
        log.error("regime assessment failed: %s", exc)
        return RegimeReading(verdict="NEUTRAL",
                             reasons=[f"regime unavailable ({type(exc).__name__})"],
                             degraded=True)


def _cached_symbols(cache: BarCache) -> list[str]:
    import sqlite3
    from contextlib import closing
    with closing(sqlite3.connect(cache.path)) as conn:
        return [r[0] for r in conn.execute("SELECT DISTINCT symbol FROM bars")]


def _edgar():
    from mcp.news import EdgarProvider
    try:
        return EdgarProvider()
    except Exception as exc:                              # noqa: BLE001
        log.warning("EDGAR unavailable: %s", exc)
        return None


def _merge(candidates: list[dict], analyses: list[dict]) -> list[dict]:
    by_symbol = {a["symbol"]: a for a in analyses}
    out = []
    for c in candidates:
        a = by_symbol.get(c["symbol"], {})
        out.append({**c,
                    "disqualified": bool(a.get("disqualified")),
                    "disqualify_reason": a.get("disqualify_reason", ""),
                    "catalyst_summary": a.get("catalyst_summary", ""),
                    "catalyst_quality": a.get("catalyst_quality", 0),
                    "concerns": a.get("concerns", []),
                    "earnings_note": a.get("earnings_note"),
                    "agent_error": a.get("agent_error")})
    return out


def _top_eliminator(funnel: list[dict]) -> dict | None:
    rows = [r for r in funnel if r.get("eliminated", 0) > 0]
    return max(rows, key=lambda r: r["eliminated"]) if rows else None


def _cost_note(offline: bool) -> str:
    if offline:
        return "offline"
    try:
        from pipeline.llm import daily_cost_report
        report = daily_cost_report(since="1d")
        return f"${report.get('actual_cost', 0):.2f} · saved {report.get('saved_pct', 0):.0f}%"
    except Exception:                                     # noqa: BLE001
        return ""


class _NullNotifier:
    def send(self, text, **_):
        print("\n--- DRY RUN, message not sent ---\n" + text + "\n---\n")
        return True

    def alert(self, text):
        print(f"[alert] {text}")
        return True


def _notifier(dry_run: bool):
    if dry_run:
        return _NullNotifier()
    try:
        from notify.telegram import Telegram
        return Telegram()
    except Exception as exc:                              # noqa: BLE001
        log.error("Telegram unavailable (%s); falling back to stdout", exc)
        return _NullNotifier()


def _deliver(text, picks, reading, summary, payload, journal, notifier,
             as_of, started, offline, dry_run) -> dict:
    sent = notifier.send(text)
    if not sent:
        log.error("Telegram delivery failed")

    record = {
        "as_of": str(as_of),
        "regime": reading.verdict,
        "regime_reasons": reading.reasons,
        "regime_metrics": reading.metrics,
        "screen": payload.get("screen", {}),
        "candidates_considered": len(payload.get("candidates", [])),
        "disqualified": summary.get("disqualified", 0),
        "picks": [{"symbol": p["symbol"], "thesis": p.get("thesis", ""),
                   "conviction": p.get("conviction"),
                   "invalidation": p.get("invalidation", ""),
                   "plan": p.get("plan", {})} for p in picks],
        "delivered": bool(sent),
        "offline": offline,
        "dry_run": dry_run,
        "message": text,
    }
    journal.record_brief(record)

    beat = {"stage": "B",
            "completed_at": datetime.now().astimezone().isoformat(),
            "picks": len(picks), "regime": reading.verdict,
            "delivered": bool(sent),
            "duration_seconds": round(time.monotonic() - started, 1)}
    (paths.DATA_DIR / "heartbeat_b.json").write_text(json.dumps(beat, indent=2))

    log.info("brief delivered: %s, %d picks, %.1fs",
             reading.verdict, len(picks), time.monotonic() - started)
    return record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stage B: the morning brief")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S")

    try:
        run(offline=args.offline, dry_run=args.dry_run)
        return 0
    except Exception as exc:                              # noqa: BLE001
        log.error("stage B failed: %s", exc)
        log.debug(traceback.format_exc())
        # The brief is due now. Send SOMETHING -- an alert that says the brief
        # failed is worth far more than silence you might read as "no setups".
        try:
            from notify.telegram import Telegram
            Telegram().alert(f"Morning brief FAILED: {type(exc).__name__}: {exc}")
        except Exception:                                 # noqa: BLE001
            log.error("could not send failure alert either")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
