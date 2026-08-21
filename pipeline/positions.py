#!/usr/bin/env python3
"""Live position tracking and the daily sell check.

    python3 -m pipeline.positions              # what do I hold, what should I sell
    python3 -m pipeline.positions --send       # push the answer to Telegram
    python3 -m pipeline.positions --offline    # synthetic, no keys

Positions are derived from the journal, which is populated by replying to the
morning brief:

    TOOK AAPL 100 @ 182.50      -> opens a position
    SOLD AAPL 100 @ 195.00      -> closes it

So "when I say I make a buy" is a Telegram reply, and nothing else has to be
kept in sync by hand.

The stop and target for a tracked position come from the brief that recommended
it. A name bought off-plan (no matching recommendation) still gets tracked, with
a stop derived from its current ATR -- because an untracked position is the one
that will hurt you, and refusing to track it would be the worst possible
response to a slightly irregular input.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.exits import (SELL, SEVERITY, TRIM, WATCH, ExitConfig,  # noqa: E402
                        Position, evaluate_book)
from mcp.journal import Journal                                    # noqa: E402
from pipeline import paths                                         # noqa: E402

log = logging.getLogger("positions")

DEFAULT_STOP_PCT = 8.0      # fallback for off-plan buys with no ATR available


# --------------------------------------------------------------------------- #
# reconstruct the book
# --------------------------------------------------------------------------- #

def open_positions(journal: Journal, *, plans: dict[str, dict] | None = None,
                   quotes: dict[str, dict] | None = None) -> list[Position]:
    """FIFO-net the fills into current holdings.

    Deliberately simple netting: buys accumulate, sells consume oldest-first.
    Anything left is open. Same conservatism as the weekly review -- an
    unmatched sell is ignored rather than guessed at.
    """
    plans = plans or {}
    quotes = quotes or {}
    fills = sorted(journal.read_fills(), key=lambda f: f.get("logged_at", ""))

    lots: dict[str, list[dict]] = {}
    for fill in fills:
        symbol = fill.get("symbol")
        if not symbol or not fill.get("quantity"):
            continue
        queue = lots.setdefault(symbol, [])
        if fill.get("side") == "buy":
            queue.append({"shares": float(fill["quantity"]),
                          "price": float(fill["price"]),
                          "on": _fill_date(fill)})
        else:
            remaining = float(fill["quantity"])
            while remaining > 0 and queue:
                lot = queue[0]
                matched = min(remaining, lot["shares"])
                lot["shares"] -= matched
                remaining -= matched
                if lot["shares"] <= 1e-9:
                    queue.pop(0)

    positions = []
    for symbol, queue in lots.items():
        shares = sum(lot["shares"] for lot in queue)
        if shares <= 1e-9:
            continue
        cost = sum(lot["shares"] * lot["price"] for lot in queue) / shares
        opened = min(lot["on"] for lot in queue)
        plan = plans.get(symbol, {})
        stop, target = _levels(plan, cost, quotes.get(symbol, {}))
        positions.append(Position(
            symbol=symbol, entry_date=opened, entry_price=cost, shares=shares,
            stop=stop, target=target, initial_stop=stop,
            sector=plan.get("sector", "UNKNOWN"),
            thesis=plan.get("thesis", ""),
            invalidation=plan.get("invalidation", "")))
    return sorted(positions, key=lambda p: p.symbol)


def _levels(plan: dict, cost: float, quote: dict) -> tuple[float, float]:
    """Stop and target: from the plan if we recommended it, else derived."""
    stop = plan.get("plan", {}).get("stop") if "plan" in plan else plan.get("stop")
    target = plan.get("plan", {}).get("target") if "plan" in plan else plan.get("target")
    if stop and target:
        return float(stop), float(target)

    atr = quote.get("atr")
    if atr:
        stop = cost - 2.0 * float(atr)
    else:
        stop = cost * (1 - DEFAULT_STOP_PCT / 100.0)
    return stop, cost + (cost - stop) * 2.5


def plans_from_briefs(journal: Journal) -> dict[str, dict]:
    """The most recent recommendation for each symbol, for stops and targets."""
    plans: dict[str, dict] = {}
    for brief in journal.read_briefs():
        for pick in brief.get("picks", []):
            plans[pick["symbol"]] = pick
    return plans


def _fill_date(fill: dict) -> date:
    for key in ("message_date", "logged_at"):
        value = fill.get(key)
        if value:
            try:
                return date.fromisoformat(str(value)[:10])
            except ValueError:
                continue
    return date.today()


# --------------------------------------------------------------------------- #
# quotes
# --------------------------------------------------------------------------- #

def quotes_for(symbols: list[str], *, offline: bool = False) -> dict[str, dict]:
    """Latest close, high, low, ATR, MA50 and RS rank for the held names."""
    if not symbols:
        return {}
    if offline:
        from core import synthetic as syn
        bars = {s: syn.stage2(n=320, seed=abs(hash(s)) % 999) for s in symbols}
    else:
        from mcp.market_data import BarCache
        cache = BarCache(paths.BARS_DB)
        bars = {s: b for s, b in cache.read_many(symbols).items() if not b.empty}

    from core.indicators import atr as atr_fn
    from core.indicators import sma

    out: dict[str, dict] = {}
    for symbol, frame in bars.items():
        if frame.empty:
            continue
        last = frame.iloc[-1]
        close = float(last["close"])
        ma50 = sma(frame["close"], 50).iloc[-1] if len(frame) >= 50 else None
        atr_series = atr_fn(frame, 14)
        out[symbol] = {
            "close": close, "open": float(last["open"]),
            "high": float(last["high"]), "low": float(last["low"]),
            "atr": _f(atr_series.iloc[-1]) if len(atr_series) else None,
            "ma50": _f(ma50),
        }
    return out


def _f(value):
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return None if out != out else out


# --------------------------------------------------------------------------- #
# report
# --------------------------------------------------------------------------- #

def render(positions: list[Position], signals: list, quotes: dict,
           as_of: date) -> str:
    if not positions:
        return "No open positions.\n\nReply `TOOK TICKER QTY @ PRICE` to a brief to open one."

    total_cost = sum(p.shares * p.entry_price for p in positions)
    total_value = sum(p.shares * quotes.get(p.symbol, {}).get("close", p.entry_price)
                      for p in positions)
    unrealised = total_value - total_cost

    lines = [f"POSITIONS — {as_of}", "=" * 60,
             f"  {len(positions)} open · cost ${total_cost:,.2f} · "
             f"value ${total_value:,.2f} · "
             f"unrealised ${unrealised:+,.2f} ({unrealised / total_cost * 100:+.1f}%)"
             if total_cost else "  no cost basis", ""]

    urgent = [s for s in signals if s.action in (SELL, TRIM)]
    if urgent:
        lines.append("  ACTION NEEDED")
        for signal in urgent:
            lines.append(f"    {signal.action:<5} {signal.symbol:<8} {signal.detail}")
        lines.append("")

    lines.append("  BOOK")
    for signal in signals:
        position = next((p for p in positions if p.symbol == signal.symbol), None)
        if position is None:
            continue
        price = quotes.get(signal.symbol, {}).get("close", position.entry_price)
        flag = {SELL: "!!", TRIM: "! ", WATCH: "? "}.get(signal.action, "  ")
        lines.append(
            f"   {flag}{position.symbol:<8} {position.shares:>9.4f} sh @ "
            f"{position.entry_price:>8.2f}  now {price:>8.2f}  "
            f"{position.r_multiple(price):+5.2f}R  "
            f"{position.pct_change(price):+6.1f}%  "
            f"stop {position.stop:>7.2f}  {signal.reason}")
        if signal.suggested_stop and signal.action not in (SELL,):
            lines.append(f"      raise stop to {signal.suggested_stop:.2f}")
    return "\n".join(lines)


def run(*, offline: bool = False, send: bool = False) -> dict:
    paths.ensure_dirs()
    paths.load_dotenv()

    journal = Journal(paths.JOURNAL_DIR)
    plans = plans_from_briefs(journal)
    bootstrap = open_positions(journal, plans=plans)
    quotes = quotes_for([p.symbol for p in bootstrap], offline=offline)
    # Second pass: off-plan names need a quote before their stop can be derived
    # from ATR, and the quote lookup needs the symbol list from the first pass.
    positions = open_positions(journal, plans=plans, quotes=quotes)

    as_of = date.today()
    signals = evaluate_book(positions, quotes, as_of=as_of, config=ExitConfig.from_yaml())
    text = render(positions, signals, quotes, as_of)
    print(text)

    if send and positions:
        try:
            from notify.telegram import Telegram
            Telegram().send(text, parse_mode="")
        except Exception as exc:                          # noqa: BLE001
            log.error("could not send: %s", exc)

    return {"as_of": str(as_of),
            "positions": [p.to_dict() for p in positions],
            "signals": [s.to_dict() for s in signals],
            "urgent": [s.symbol for s in signals if s.action in (SELL, TRIM)]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Open positions and sell signals")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--send", action="store_true", help="push to Telegram")
    parser.add_argument("--json", type=Path, default=None)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")
    try:
        out = run(offline=args.offline, send=args.send)
        if args.json:
            args.json.parent.mkdir(parents=True, exist_ok=True)
            args.json.write_text(json.dumps(out, indent=2, default=str))
        return 0
    except Exception as exc:                              # noqa: BLE001
        log.error("position check failed: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
