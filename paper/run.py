#!/usr/bin/env python3
"""The one-month paper test.

    python3 -m paper.run --offline                    # simulate a month instantly
    python3 -m paper.run --weeks 4 --deposit 100      # the configured test
    python3 -m paper.run --step                       # advance one day (forward mode)

Deposits $100 every Monday for four weeks. Unused cash carries over. Runs the
real screen, the real sizing and the real exit rules against a simulated
account, then reports what happened.

TWO MODES
---------
**Simulated** (default with --offline, or --end-date on cached bars): replays a
month of history in seconds. You get an answer today.

**Forward**: `--step` advances one day using the live cache and persists the
account. Run it from the same timer as Stage A to accumulate a real forward
test. Slower, and the only version that is genuinely out-of-sample.

WHAT THIS DOES AND DOES NOT PROVE
---------------------------------
One month is roughly 21 trading days. At six positions and a 1-4 week hold that
is perhaps 5-15 round trips. **That is not enough to measure an edge** -- the
confidence interval on a win rate from 10 trades runs from "excellent" to
"terrible". Treat the P&L as noise.

What a month DOES test, and what it is worth running for:

  - does the machinery work unattended, end to end, every day
  - does the account ever get stuck (all cash, no affordable entries)
  - do the exit rules fire when they should, and at sane prices
  - is the brief something you would actually read at 5 AM
  - how often does the regime filter stand you down

Those are process questions, and a month answers them well.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd  # noqa: E402

from backtest.panels import build_panels                       # noqa: E402
from core.exits import SEVERITY, ExitConfig                     # noqa: E402
from core.ranking import RankConfig, top_candidates             # noqa: E402
from core.regime import RegimeConfig, apply_regime              # noqa: E402
from core.regime import assess as assess_regime                 # noqa: E402
from core.screen import ScreenConfig, evaluate                  # noqa: E402
from core.sizing import Portfolio, SizingConfig, size_candidates  # noqa: E402
from paper.account import Account                               # noqa: E402
from paper.broker import ExecutionConfig, PaperBroker           # noqa: E402

log = logging.getLogger("paper")

DEFAULT_STATE = Path("data/paper/account.json")


# --------------------------------------------------------------------------- #

def deposit_dates(start: date, weeks: int) -> list[date]:
    """One deposit per week, on the Monday of each week including the first."""
    monday = start - timedelta(days=start.weekday())
    return [monday + timedelta(weeks=w) for w in range(weeks)]


def _quotes_for(panels, when, symbols) -> dict[str, dict]:
    """Today's marks for the exit rules: OHLC plus ATR, MA50 and RS rank."""
    out: dict[str, dict] = {}
    metrics = None
    for symbol in symbols:
        if symbol not in panels.closes.columns:
            continue
        try:
            close = panels.closes.at[when, symbol]
        except KeyError:
            continue
        if pd.isna(close):
            continue
        quote = {"close": float(close),
                 "open": _safe(panels.opens, when, symbol),
                 "high": _safe(panels.highs, when, symbol),
                 "low": _safe(panels.lows, when, symbol)}
        if metrics is None:
            metrics = panels.metrics_on(when)
        if symbol in metrics.index:
            row = metrics.loc[symbol]
            quote["atr"] = _num(row.get("atr"))
            quote["ma50"] = _num(row.get("ma50"))
            quote["rs_rank"] = _num(row.get("rs_rank"))
        out[symbol] = quote
    return out


def _safe(frame, when, symbol):
    try:
        value = frame.at[when, symbol]
    except KeyError:
        return None
    return None if pd.isna(value) else float(value)


def _num(value):
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return None if out != out else out


def _decide(panels, when, account, equity, reading, screen_cfg, rank_cfg,
            sizing_cfg) -> list[dict]:
    """Run the production screen and produce orders for the next open."""
    metrics = panels.metrics_on(when)
    if metrics.empty:
        return []
    result = evaluate(metrics, screen_cfg)
    ranked = top_candidates(result, rank_cfg)
    if ranked.empty:
        return []

    sizing = apply_regime(reading, sizing_cfg)
    # Size against the equity the account actually has, and enable fractional
    # shares: at a few hundred dollars the whole-share model returns zero.
    sizing = replace(sizing, account_equity=equity, allow_fractional=True)

    book = Portfolio(
        open_risk_dollars=sum(p.shares * p.risk_per_share
                              for p in account.positions.values()),
        positions={s: p.shares * p.risk_per_share
                   for s, p in account.positions.items()},
        sectors={},
    )
    sized = size_candidates(ranked, book, sizing)
    taken = sized[sized["sizable"]] if not sized.empty else sized

    orders = []
    for symbol, row in taken.iterrows():
        if symbol in account.positions:
            continue
        entry, stop = float(row["entry"]), float(row["stop"])
        orders.append({"symbol": symbol, "shares": float(row["shares"]),
                       "risk_per_share": entry - stop,
                       "reward_per_share": float(row["target"]) - entry,
                       "sector": str(row.get("sector", "UNKNOWN"))})
    return orders


def simulate(panels, *, start: date, weeks: int = 4, deposit: float = 100.0,
             slippage: float = 0.05, use_regime: bool = True) -> dict:
    """Replay `weeks` weeks of history against a fresh paper account."""
    screen_cfg = ScreenConfig.from_yaml()
    rank_cfg = RankConfig.from_yaml()
    sizing_cfg = SizingConfig.from_yaml()
    regime_cfg = RegimeConfig.from_yaml()
    exit_cfg = ExitConfig.from_yaml()

    account = Account(allow_fractional=True)
    broker = PaperBroker(account, ExecutionConfig(slippage_pct=slippage), exit_cfg)

    end = start + timedelta(weeks=weeks)
    dates = [d for d in panels.dates if start <= d.date() <= end]
    if len(dates) < 5:
        raise ValueError(f"only {len(dates)} trading days in the window")

    scheduled = set(deposit_dates(start, weeks))
    pending: list[dict] = []
    regime_days: dict[str, int] = {}
    daily_log: list[dict] = []
    deposited_weeks = 0

    for when in dates:
        today = when.date()

        # -- 1. deposit on the first session of each scheduled week ---------
        week_monday = today - timedelta(days=today.weekday())
        if week_monday in scheduled:
            account.deposit(deposit, today)
            scheduled.discard(week_monday)
            deposited_weeks += 1
            log.info("%s  deposit $%.2f (cash now $%.2f)", today, deposit, account.cash)

        # -- 2. fill yesterday's orders at today's open ---------------------
        opens = {o["symbol"]: _safe(panels.opens, when, o["symbol"]) for o in pending}
        entry_fills = broker.execute_entries(pending, opens, today) if pending else []
        pending = []

        # -- 3. manage exits -------------------------------------------------
        quotes = _quotes_for(panels, when, list(account.positions))
        exit_fills, signals = broker.manage_exits(quotes, today)

        # -- 4. mark to market -----------------------------------------------
        prices = {s: q["close"] for s, q in quotes.items()}
        snapshot = account.mark(today, prices)

        # -- 5. decide tomorrow ----------------------------------------------
        reading = (_regime(panels, when, regime_cfg) if use_regime
                   else _always_on())
        regime_days[reading.verdict] = regime_days.get(reading.verdict, 0) + 1
        if reading.tradeable and when != dates[-1]:
            pending = _decide(panels, when, account, snapshot["equity"], reading,
                              screen_cfg, rank_cfg, sizing_cfg)

        daily_log.append({
            **snapshot, "regime": reading.verdict,
            "entries": [f.symbol for f in entry_fills],
            "exits": [(f.symbol, f.reason) for f in exit_fills],
            "watch": [s.symbol for s in signals if s.action == "WATCH"],
            "orders_queued": len(pending),
        })

    final_prices = {s: q["close"] for s, q in
                    _quotes_for(panels, dates[-1], list(account.positions)).items()}
    return {"account": account, "daily": daily_log, "regime_days": regime_days,
            "final_prices": final_prices, "weeks_deposited": deposited_weeks,
            "start": dates[0].date(), "end": dates[-1].date()}


def _regime(panels, when, cfg):
    closes = panels.closes.loc[:when]
    if len(closes) < 200:
        from core.regime import RegimeReading
        return RegimeReading(verdict="NEUTRAL", reasons=["short history"], degraded=True)
    universe = closes.drop(columns=[cfg.benchmark], errors="ignore")
    ma200 = universe.rolling(200, min_periods=200).mean().iloc[-1]
    last = universe.iloc[-1]
    valid = ma200.notna() & last.notna()
    breadth = float((last[valid] > ma200[valid]).mean() * 100) if valid.any() else None

    if cfg.benchmark not in closes:
        from core.regime import RegimeReading
        return RegimeReading(verdict="NEUTRAL", reasons=["no benchmark"], degraded=True)
    bench = closes[cfg.benchmark].dropna()
    frame = pd.DataFrame({"close": bench, "open": bench, "high": bench,
                          "low": bench, "volume": 0.0})
    return assess_regime({}, benchmark_bars=frame, config=cfg, breadth=breadth)


def _always_on():
    from core.regime import RegimeReading
    return RegimeReading(verdict="RISK_ON", reasons=["regime filter disabled"])


# --------------------------------------------------------------------------- #

def report(outcome: dict, deposit: float, weeks: int) -> str:
    account = outcome["account"]
    prices = outcome["final_prices"]
    equity = account.equity(prices)
    profit = account.profit(prices)
    closed = account.closed

    lines = ["=" * 68,
             f"PAPER TEST — ${deposit:.0f}/week for {weeks} weeks",
             "=" * 68,
             f"  period              {outcome['start']} -> {outcome['end']}",
             f"  deposits            {outcome['weeks_deposited']} x ${deposit:.0f} "
             f"= ${account.deposited:,.2f}",
             f"  ending equity       ${equity:,.2f}",
             f"  cash                ${account.cash:,.2f}",
             f"  open positions      {len(account.positions)}",
             f"  profit / loss       ${profit:+,.2f}  "
             f"({account.return_pct(prices):+.2f}% on deposits)",
             "",
             f"  round trips closed  {len(closed)}",
             f"  regime days         {outcome['regime_days']}"]

    if closed:
        wins = [t for t in closed if t.pnl > 0]
        rs = [t.r_multiple for t in closed]
        reasons: dict[str, int] = {}
        for t in closed:
            reasons[t.reason] = reasons.get(t.reason, 0) + 1
        lines += [f"  win rate            {len(wins) / len(closed) * 100:.0f}% "
                  f"({len(wins)}/{len(closed)})",
                  f"  average R           {sum(rs) / len(rs):+.2f}R",
                  f"  best / worst        {max(rs):+.2f}R / {min(rs):+.2f}R",
                  f"  exit reasons        {reasons}",
                  f"  costs paid          ${sum(t.costs for t in closed):,.2f}"]

    if account.rejected:
        lines += ["", f"  orders rejected for cash: {len(account.rejected)}",
                  "  ^ the account was too small for the position sizes the "
                  "screen wanted"]

    if closed:
        lines += ["", "  " + "-" * 64, "  TRADES"]
        for t in sorted(closed, key=lambda t: t.exit_date):
            lines.append(
                f"    {t.symbol:<8} {t.entry_date} -> {t.exit_date}  "
                f"{t.entry_price:8.2f} -> {t.exit_price:8.2f}  "
                f"{t.r_multiple:+6.2f}R  ${t.pnl:+8.2f}  {t.reason}")

    if account.positions:
        lines += ["", "  " + "-" * 64, "  STILL OPEN"]
        for symbol, p in account.positions.items():
            mark = prices.get(symbol, p.entry_price)
            lines.append(
                f"    {symbol:<8} {p.shares:8.4f} sh @ {p.entry_price:8.2f}  "
                f"now {mark:8.2f}  {p.r_multiple(mark):+5.2f}R  "
                f"stop {p.stop:.2f}")

    lines += ["", "  " + "-" * 64,
              "  A month is 21 sessions and perhaps 5-15 round trips. The",
              "  confidence interval on a win rate that small runs from",
              "  excellent to terrible, so treat the P&L as noise. What this",
              "  run does test is whether the machinery works unattended, the",
              "  exits fire at sane prices, and the account ever gets stuck."]
    return "\n".join(lines)


def _scan(panels, args) -> int:
    """Run every non-overlapping 4-week window and summarise.

    One month is one sample. This prints the distribution so the single-month
    number can be read for what it is -- a draw, not a measurement.
    """
    usable = [d for d in panels.dates if d >= panels.dates[0] + pd.Timedelta(days=300)]
    step = args.weeks * 5
    rows = []
    for i in range(0, len(usable) - step, step):
        start = usable[i].date()
        try:
            outcome = simulate(panels, start=start, weeks=args.weeks,
                               deposit=args.deposit, slippage=args.slippage,
                               use_regime=not args.no_regime)
        except ValueError:
            continue
        account = outcome["account"]
        prices = outcome["final_prices"]
        rows.append({
            "start": str(start),
            "profit": round(account.profit(prices), 2),
            "return_pct": round(account.return_pct(prices), 2),
            "trades": len(account.closed),
            "regime": max(outcome["regime_days"], key=outcome["regime_days"].get),
            "risk_off_days": outcome["regime_days"].get("RISK_OFF", 0),
        })

    if not rows:
        print("no usable windows")
        return 1

    profits = [r["profit"] for r in rows]
    traded = [r for r in rows if r["trades"] > 0]
    print("=" * 68)
    print(f"WINDOW SCAN — every {args.weeks}-week window, ${args.deposit:.0f}/week")
    print("=" * 68)
    print(f"  {'start':<12}{'profit':>10}{'return':>9}{'trades':>8}"
          f"{'dominant regime':>18}")
    for r in rows:
        print(f"  {r['start']:<12}{r['profit']:>+10.2f}{r['return_pct']:>8.1f}%"
              f"{r['trades']:>8}{r['regime']:>18}")
    print()
    print(f"  windows                {len(rows)}")
    print(f"  windows that traded    {len(traded)}")
    print(f"  best / worst month     ${max(profits):+.2f} / ${min(profits):+.2f}")
    print(f"  median month           ${sorted(profits)[len(profits) // 2]:+.2f}")
    print(f"  profitable months      "
          f"{sum(1 for p in profits if p > 0)}/{len(profits)}")
    print()
    print("  The spread between best and worst month is the point. A single")
    print("  month tells you almost nothing about the edge and quite a lot")
    print("  about whether the machinery runs.")

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(rows, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="One-month paper test")
    parser.add_argument("--offline", action="store_true",
                        help="synthetic market; no keys or network")
    parser.add_argument("--weeks", type=int, default=4)
    parser.add_argument("--deposit", type=float, default=100.0)
    parser.add_argument("--slippage", type=float, default=0.05)
    parser.add_argument("--symbols", type=int, default=60)
    parser.add_argument("--no-regime", action="store_true")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--start", type=date.fromisoformat, default=None,
                        help="first day of the test window (default: the last "
                             "N weeks of available data)")
    parser.add_argument("--scan", action="store_true",
                        help="run every 4-week window and summarise — shows how "
                             "much a single month's result depends on which "
                             "month you happened to pick")
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--json", type=Path, default=None)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")

    if args.offline:
        from core import synthetic as syn
        bars, sectors = syn.market(n_symbols=args.symbols, n_days=900, seed=args.seed)
    else:
        from mcp.market_data import BarCache
        from mcp.market_data.universe import load_sectors
        from pipeline import paths
        cache = BarCache(paths.BARS_DB)
        import sqlite3
        from contextlib import closing
        with closing(sqlite3.connect(cache.path)) as conn:
            names = [r[0] for r in conn.execute("SELECT DISTINCT symbol FROM bars")]
        if not names:
            raise SystemExit("bar cache is empty — run stage_a_nightly first, "
                             "or use --offline")
        bars = {s: b for s, b in cache.read_many(names).items() if not b.empty}
        sectors = load_sectors()

    panels = build_panels(bars, sectors=sectors)
    if args.scan:
        return _scan(panels, args)

    start = args.start or (panels.dates[-1] - pd.Timedelta(weeks=args.weeks)).date()
    log.info("panels: %d dates; testing from %s", len(panels.dates), start)

    outcome = simulate(panels, start=start, weeks=args.weeks, deposit=args.deposit,
                       slippage=args.slippage, use_regime=not args.no_regime)
    text = report(outcome, args.deposit, args.weeks)
    print(text)

    args.state.parent.mkdir(parents=True, exist_ok=True)
    outcome["account"].save(args.state)
    log.info("account state written to %s", args.state)

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(
            {"daily": outcome["daily"], "regime_days": outcome["regime_days"],
             "account": outcome["account"].to_dict()}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
