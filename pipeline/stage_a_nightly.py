#!/usr/bin/env python3
"""Stage A — 01:00 Pacific. Everything deterministic, nothing that costs money.

    python3 -m pipeline.stage_a_nightly [--offline] [--as-of 2026-08-19]

Refreshes the bar cache, computes indicators, runs the Trend Template, ranks
and sizes the survivors, and writes candidates.json for Stage B to read four
hours later.

It runs at 01:00 rather than 05:00 for one reason: if it fails, there are four
hours to notice and fix it before the brief is due. That margin is the whole
argument for splitting the pipeline in two.
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

from core.ranking import RankConfig, top_candidates                  # noqa: E402
from core.regime import RegimeConfig                                 # noqa: E402
from core.screen import ScreenConfig, evaluate                       # noqa: E402
from core.sizing import Portfolio, SizingConfig, portfolio_summary, size_candidates  # noqa: E402
from mcp.market_data import get_bars, resolve_provider               # noqa: E402
from mcp.market_data.universe import (load_sectors, load_universe,   # noqa: E402
                                      offline_universe)
from pipeline import paths                                           # noqa: E402

log = logging.getLogger("stage_a")

SCHEMA_VERSION = 1


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def run(offline: bool = False, as_of: date | None = None,
        max_symbols: int | None = None) -> dict:
    started = time.monotonic()
    paths.ensure_dirs()
    paths.load_dotenv()

    screen_cfg = ScreenConfig.from_yaml()
    rank_cfg = RankConfig.from_yaml()
    size_cfg = SizingConfig.from_yaml()

    # -- universe ----------------------------------------------------------
    if offline:
        symbols, sectors = offline_universe(60), {}
    else:
        symbols, sectors = load_universe(), load_sectors()
    if max_symbols:
        symbols = symbols[:max_symbols]

    # The regime benchmark is an ETF, so it is NOT in the S&P 500 constituent
    # list and would never be fetched. Stage B then computes the regime with no
    # benchmark, degrades to NEUTRAL every single day, and the RISK_OFF branch
    # becomes unreachable -- a broken off-switch that looks like a working one.
    regime_cfg = RegimeConfig.from_yaml()
    benchmark = regime_cfg.benchmark
    if benchmark not in symbols:
        symbols = symbols + [benchmark]
        log.info("added regime benchmark %s to the fetch list", benchmark)

    log.info("universe: %d symbols", len(symbols))

    # -- bars --------------------------------------------------------------
    provider = resolve_provider("offline" if offline else None)
    bundle = get_bars(
        symbols, provider=provider, cache_path=paths.BARS_DB, as_of=as_of,
        on_progress=lambda i, n: log.info("  fetched %d/%d", i, n),
    )
    log.info("bars: %s", {k: v for k, v in bundle.stats.items() if k != "failures"})
    if bundle.failed:
        log.warning("%d symbol(s) failed to fetch: %s",
                    len(bundle.failed), list(bundle.failed)[:10])

    # -- screen ------------------------------------------------------------
    from core.indicators import build_metrics_frame

    metrics = build_metrics_frame(
        bundle.bars,
        as_of=None if as_of is None else __import__("pandas").Timestamp(as_of),
        ma200_lookback=screen_cfg.ma200_lookback,
    )
    if sectors:
        metrics["sector"] = [sectors.get(s, "UNKNOWN") for s in metrics.index]
    if benchmark in metrics.index:
        metrics = metrics.drop(index=benchmark)   # fetch it, do not screen it
    result = evaluate(metrics, screen_cfg)
    log.info("screen: %s", result.summary())

    # -- rank and size -----------------------------------------------------
    ranked = top_candidates(result, rank_cfg)
    sized = size_candidates(ranked, Portfolio.empty(), size_cfg)
    log.info("candidates: %d ranked, %d sizable", len(ranked), int(sized["sizable"].sum())
             if not sized.empty else 0)

    payload = _build_payload(result, sized, bundle, screen_cfg, size_cfg,
                             as_of or date.today(), time.monotonic() - started)
    _write_json(paths.CANDIDATES, payload)
    _write_json(paths.HEARTBEAT, {
        "stage": "A", "completed_at": datetime.now().astimezone().isoformat(),
        "candidates": len(payload["candidates"]),
        "duration_seconds": payload["run"]["duration_seconds"],
    })
    log.info("wrote %s (%d candidates) in %.1fs",
             paths.CANDIDATES, len(payload["candidates"]), time.monotonic() - started)
    return payload


def _build_payload(result, sized, bundle, screen_cfg, size_cfg, as_of, elapsed) -> dict:
    """The Stage A -> Stage B contract.

    Deliberately includes the funnel and the rejected names, not just the
    winners. Stage B's brief is far more useful when it can say "nothing passed,
    and here is what killed it" than when it can only say "nothing passed".
    """
    candidates = []
    if not sized.empty:
        for symbol, row in sized.iterrows():
            candidates.append({
                "symbol": symbol,
                "sector": str(row.get("sector", "UNKNOWN")),
                "close": _f(row.get("close")),
                "rs_rank": _f(row.get("rs_rank")),
                "pct_below_52wk_high": _f(row.get("pct_below_52wk_high")),
                "ma200_slope_pct": _f(row.get("ma200_slope_pct")),
                "volume_trend": _f(row.get("volume_trend")),
                "atr": _f(row.get("atr")),
                "atr_pct": _f(row.get("atr_pct")),
                "avg_volume": _f(row.get("avg_volume")),
                "rank_score": _f(row.get("rank_score")),
                "plan": _plan(row),
            })

    return {
        "schema_version": SCHEMA_VERSION,
        "run": {
            "stage": "A",
            "as_of": str(as_of),
            "generated_at": datetime.now().astimezone().isoformat(),
            "duration_seconds": round(elapsed, 1),
        },
        "data": {k: v for k, v in bundle.stats.items() if k != "failures"},
        "data_failures": bundle.failed,
        "screen": result.summary(),
        "funnel": result.funnel().to_dict("records"),
        "config": {
            "min_price": screen_cfg.min_price,
            "min_avg_volume": screen_cfg.min_avg_volume,
            "max_pct_below_52wk_high": screen_cfg.max_pct_below_52wk_high,
            "min_rs_rank": screen_cfg.min_rs_rank,
            "max_candidates": screen_cfg.max_candidates,
            "account_equity": size_cfg.account_equity,
            "risk_per_trade_pct": size_cfg.risk_per_trade_pct,
        },
        "portfolio": portfolio_summary(sized, size_cfg),
        "candidates": candidates,
    }


def _plan(row) -> dict:
    """Serialise the trade plan at money precision, and make it self-consistent.

    Entry, stop and target are order prices -- two decimals, because that is
    what you can actually enter. Risk and notional are then derived from those
    ROUNDED values rather than from full-precision internals, so anyone who
    recomputes shares x (entry - stop) from the file gets exactly the number
    the file reports. A brief whose own arithmetic does not tie is a brief you
    start double-checking, and then stop trusting.
    """
    shares = _shares(row.get("shares"))
    entry = _money(row.get("entry"))
    stop = _money(row.get("stop"))
    target = _money(row.get("target"))
    risk = round(shares * (entry - stop), 2) if (entry is not None and stop is not None) else None
    value = round(shares * entry, 2) if entry is not None else None
    return {
        "sizable": bool(row.get("sizable", False)),
        "entry": entry,
        "stop": stop,
        "target": target,
        "shares": shares,
        "risk_dollars": risk,
        "position_value": value,
        "r_multiple": _f(row.get("r_multiple")),
        "binding_cap": row.get("binding_cap"),
        "rejection": row.get("sizing_rejection"),
    }


def _shares(value) -> float:
    """Share counts are floats now that fractional sizing exists. Whole-share
    configs still produce integral values; this just stops int() from silently
    truncating 0.37 shares to zero."""
    try:
        out = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    return round(out, 6)


def _money(value) -> float | None:
    out = _f(value)
    return None if out is None else round(out, 2)


def _f(value) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return None if out != out else round(out, 4)      # NaN -> null


def _write_json(path: Path, payload: dict) -> None:
    """Atomic write. A half-written candidates.json read by Stage B at 05:00
    would be worse than no file at all, because it would parse."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str))
    tmp.replace(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stage A: nightly screen")
    parser.add_argument("--offline", action="store_true",
                        help="synthetic data, no API keys or network")
    parser.add_argument("--as-of", type=date.fromisoformat, default=None,
                        help="run as if today were this date (historical replay)")
    parser.add_argument("--max-symbols", type=int, default=None)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    _configure_logging(args.verbose)
    try:
        run(offline=args.offline, as_of=args.as_of, max_symbols=args.max_symbols)
        return 0
    except Exception as exc:                          # noqa: BLE001
        log.error("stage A failed: %s", exc)
        log.debug(traceback.format_exc())
        # Alert here as well as in systemd's ExecStopPost: this path knows what
        # actually broke, and a message at 01:00 gives four hours to fix it.
        try:
            from notify.telegram import Telegram
            Telegram().alert(f"Stage A failed: {type(exc).__name__}: {exc}")
        except Exception:                             # noqa: BLE001
            log.error("could not send failure alert")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
