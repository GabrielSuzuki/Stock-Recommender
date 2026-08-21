#!/usr/bin/env python3
"""Preflight — check everything before the first live run.

    python3 -m pipeline.preflight
    python3 -m pipeline.preflight --fix-volume-scale   # write the measured value

Nothing in this repo has ever touched a real price. This is the script that
finds out what breaks when it does, in about ninety seconds, instead of at
05:00 on a Tuesday.

It checks, in order of how likely each is to be the thing that bites:

    1. every credential, individually, with the actual API
    2. **the IEX volume scale** -- the single most likely thing to be wrong
    3. the universe fetch and the benchmark
    4. the bar cache, disk, timezone and clock
    5. an end-to-end dry run of both stages

Exit code is 0 only when nothing is BLOCKED.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import statistics
import sys
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import paths  # noqa: E402

log = logging.getLogger("preflight")

OK, WARN, BLOCK, SKIP = "OK", "WARN", "BLOCK", "SKIP"
ICON = {OK: "✓", WARN: "!", BLOCK: "✗", SKIP: "-"}

# Large, continuously liquid names whose real consolidated ADV is easy to
# sanity-check. Used only to measure the IEX ratio, never screened.
VOLUME_PROBES = ("AAPL", "MSFT", "NVDA", "AMZN", "SPY")

# Rough consolidated 50-day average volume, in millions of shares. These are
# order-of-magnitude reference points, not live figures -- the ratio only needs
# to be right to within a factor of two to be actionable.
REFERENCE_ADV_M = {"AAPL": 55.0, "MSFT": 22.0, "NVDA": 250.0,
                   "AMZN": 45.0, "SPY": 75.0}


@dataclass
class Check:
    name: str
    status: str
    detail: str = ""
    fix: str = ""
    data: dict = field(default_factory=dict)


class Preflight:
    def __init__(self, offline: bool = False):
        self.offline = offline
        self.checks: list[Check] = []

    def add(self, name, status, detail="", fix="", **data) -> Check:
        check = Check(name, status, detail, fix, data)
        self.checks.append(check)
        colour = {OK: "", WARN: "", BLOCK: "", SKIP: ""}[status]
        print(f"  {ICON[status]} {name:<26} {detail}{colour}")
        if fix and status in (WARN, BLOCK):
            print(f"      -> {fix}")
        return check

    # -- 1. credentials -----------------------------------------------------

    def check_env(self) -> None:
        print("\nCREDENTIALS")
        required = {
            "ALPACA_API_KEY_ID": "free paper account at alpaca.markets",
            "ALPACA_API_SECRET_KEY": "same page as the key id",
        }
        optional = {
            "TELEGRAM_BOT_TOKEN": "docs/setup-telegram.md",
            "TELEGRAM_CHAT_ID": "docs/setup-telegram.md",
            "FINNHUB_API_KEY": "finnhub.io free tier — news and earnings",
            "ANTHROPIC_API_KEY": "console.anthropic.com — needed for Stage B",
            "MARKETAUX_API_KEY": "optional; sentiment only",
        }
        for key, where in required.items():
            if os.environ.get(key, "").strip():
                self.add(key, OK, "set")
            else:
                self.add(key, BLOCK, "missing", f"get one: {where}")
        for key, where in optional.items():
            if os.environ.get(key, "").strip():
                self.add(key, OK, "set")
            else:
                self.add(key, WARN, "missing", where)

        agent = os.environ.get("SEC_USER_AGENT", "")
        if "@" in agent and len(agent.split()) >= 2:
            self.add("SEC_USER_AGENT", OK, agent[:40])
        else:
            self.add("SEC_USER_AGENT", WARN, "malformed or missing",
                     "must be 'Your Name your@email.com' or EDGAR blocks you silently")

    # -- 2. live API reachability -------------------------------------------

    def check_alpaca(self) -> dict | None:
        print("\nALPACA")
        if not (os.environ.get("ALPACA_API_KEY_ID") and
                os.environ.get("ALPACA_API_SECRET_KEY")):
            self.add("alpaca connection", SKIP, "no credentials")
            return None
        try:
            from mcp.market_data import AlpacaProvider
            provider = AlpacaProvider()
            end = date.today()
            start = end - timedelta(days=400)
            started = time.monotonic()
            bars = provider.fetch("AAPL", start, end)
            elapsed = time.monotonic() - started
        except Exception as exc:                          # noqa: BLE001
            self.add("alpaca connection", BLOCK, f"{type(exc).__name__}: {exc}",
                     "check the keys, and that alpaca-py is installed")
            return None

        if bars.empty:
            self.add("alpaca connection", BLOCK, "connected but returned no bars",
                     "check the account is activated")
            return None

        self.add("alpaca connection", OK,
                 f"{len(bars)} bars for AAPL in {elapsed:.1f}s")
        last = bars.index[-1].date()
        age = (date.today() - last).days
        if age <= 4:
            self.add("bar freshness", OK, f"newest bar {last}")
        else:
            self.add("bar freshness", WARN, f"newest bar {last} ({age}d old)",
                     "fine on a long weekend; investigate if it persists")
        return {"bars": bars}

    def check_volume_scale(self) -> float | None:
        """Measure IEX volume against consolidated reference figures.

        This is the check that matters most. Alpaca's free feed reports
        IEX-only volume -- one venue, low single-digit percent of the
        consolidated tape -- while `min_avg_volume: 400000` is calibrated for
        consolidated volume. Left uncorrected, the liquidity gate rejects
        essentially the entire S&P 500 and the screen returns an empty list
        every morning that reads exactly like a slow market.
        """
        print("\nIEX VOLUME SCALE  (the most likely thing to be wrong)")
        if not os.environ.get("ALPACA_API_KEY_ID"):
            self.add("volume scale", SKIP, "no credentials")
            return None

        try:
            from mcp.market_data import AlpacaProvider
            provider = AlpacaProvider()
        except Exception as exc:                          # noqa: BLE001
            self.add("volume scale", SKIP, str(exc))
            return None

        end = date.today()
        start = end - timedelta(days=120)
        ratios, rows = [], []
        for symbol in VOLUME_PROBES:
            try:
                bars = provider.fetch(symbol, start, end)
            except Exception as exc:                      # noqa: BLE001
                rows.append((symbol, None, None, f"fetch failed: {type(exc).__name__}"))
                continue
            if bars.empty or len(bars) < 20:
                rows.append((symbol, None, None, "insufficient bars"))
                continue
            observed = float(bars["volume"].tail(50).mean()) / 1e6
            reference = REFERENCE_ADV_M[symbol]
            ratio = observed / reference
            ratios.append(ratio)
            rows.append((symbol, observed, reference, f"{ratio * 100:.1f}% of tape"))

        for symbol, observed, reference, note in rows:
            if observed is None:
                print(f"      {symbol:<6} {note}")
            else:
                print(f"      {symbol:<6} feed {observed:8.2f}M  "
                      f"reference {reference:6.1f}M   {note}")

        if not ratios:
            self.add("volume scale", BLOCK, "could not measure",
                     "no probe symbol returned usable bars")
            return None

        median = statistics.median(ratios)
        scale = round(1.0 / median, 1)

        if median > 0.5:
            self.add("volume scale", OK,
                     f"feed is {median * 100:.0f}% of consolidated — looks like "
                     "full-tape data, no scaling needed")
            return 1.0

        spread = max(ratios) / min(ratios) if min(ratios) > 0 else float("inf")
        floor = int(round(400_000 * median, -3))

        # If the floor is already calibrated to this feed, say so rather than
        # repeating advice that has been taken. A warning that fires forever
        # after being addressed trains you to ignore warnings.
        try:
            from core.screen import ScreenConfig
            configured = ScreenConfig.from_yaml().min_avg_volume
        except Exception:                            # noqa: BLE001
            configured = None

        if configured is not None and configured <= floor * 3:
            self.add("volume scale", OK,
                     f"feed is {median * 100:.1f}% of tape; min_avg_volume is "
                     f"{configured:,.0f}, already calibrated to it",
                     median_ratio=median, configured_floor=configured)
            return 1.0

        self.add("volume scale", WARN,
                 f"feed is {median * 100:.1f}% of consolidated tape "
                 f"(per-symbol spread {spread:.1f}x)",
                 f"RECOMMENDED: set universe.min_avg_volume to about {floor:,} in "
                 f"config/screen.yaml, which calibrates the gate to the feed you "
                 f"actually have. Alternative: volume_scale={scale} "
                 f"(--fix-volume-scale writes it), but that puts an invented "
                 f"volume figure into the brief and the {spread:.1f}x spread "
                 f"across symbols makes it only accurate to a factor of two.",
                 scale=scale, median_ratio=median, suggested_floor=floor,
                 spread=round(spread, 2))
        return scale

    def check_finnhub(self) -> None:
        print("\nNEWS AND FILINGS")
        if not os.environ.get("FINNHUB_API_KEY"):
            self.add("finnhub", SKIP, "no key — Stage B briefs without news")
        else:
            try:
                from mcp.news import FinnhubProvider
                ctx = FinnhubProvider().context("AAPL", as_of=date.today(),
                                                lookback_days=7)
                if ctx.errors:
                    self.add("finnhub", WARN, f"partial: {ctx.errors}",
                             "free-tier endpoint coverage changes; check finnhub.io/pricing")
                else:
                    self.add("finnhub", OK,
                             f"{len(ctx.headlines)} headlines, earnings "
                             f"{ctx.earnings_date or 'unknown'}")
            except Exception as exc:                      # noqa: BLE001
                self.add("finnhub", WARN, f"{type(exc).__name__}: {exc}",
                         "Stage B degrades to no-news briefs")

        agent = os.environ.get("SEC_USER_AGENT", "")
        if "@" not in agent:
            self.add("edgar", SKIP, "SEC_USER_AGENT not set")
        else:
            try:
                from mcp.news import EdgarProvider
                filings = EdgarProvider().recent_filings("AAPL", as_of=date.today())
                self.add("edgar", OK, f"{len(filings)} watched filings for AAPL")
            except Exception as exc:                      # noqa: BLE001
                self.add("edgar", WARN, f"{type(exc).__name__}: {exc}",
                         "filing-based disqualify rules will not fire")

    def check_telegram(self) -> None:
        print("\nDELIVERY")
        if not (os.environ.get("TELEGRAM_BOT_TOKEN") and
                os.environ.get("TELEGRAM_CHAT_ID")):
            self.add("telegram", BLOCK, "not configured",
                     "docs/setup-telegram.md — without this the brief has nowhere to go")
            return
        try:
            from notify.telegram import Telegram
            if Telegram().send("preflight check — you can ignore this",
                               parse_mode=""):
                self.add("telegram", OK, "test message delivered")
            else:
                self.add("telegram", BLOCK, "send failed",
                         "check the token and chat id")
        except Exception as exc:                          # noqa: BLE001
            self.add("telegram", BLOCK, f"{type(exc).__name__}: {exc}",
                     "docs/setup-telegram.md")

    # -- 3. universe --------------------------------------------------------

    def check_universe(self) -> None:
        print("\nUNIVERSE")
        try:
            from core.regime import RegimeConfig
            from mcp.market_data.universe import load_universe
            symbols = load_universe()
            benchmark = RegimeConfig.from_yaml().benchmark
            if 450 <= len(symbols) <= 560:
                self.add("universe", OK, f"{len(symbols)} symbols")
            else:
                self.add("universe", WARN, f"{len(symbols)} symbols — unexpected",
                         "the source table may have changed shape")
            if benchmark in symbols:
                self.add("benchmark", OK, f"{benchmark} in the list")
            else:
                self.add("benchmark", OK,
                         f"{benchmark} is an ETF — Stage A appends it explicitly")
        except Exception as exc:                          # noqa: BLE001
            message = str(exc)
            if "HTML parser" in message or "lxml" in message:
                fix = "pip install lxml beautifulsoup4 html5lib"
            elif "no cache" in message:
                fix = ("first run needs network to fetch the S&P 500 list. If "
                       "you are behind a proxy, drop a CSV with symbol,sector "
                       "columns at the path above instead.")
            else:
                fix = "needs network on first run; cached weekly after that"
            self.add("universe", BLOCK, f"{type(exc).__name__}: {message[:160]}", fix)

    # -- 4. environment -----------------------------------------------------

    def check_environment(self) -> None:
        print("\nENVIRONMENT")
        import shutil
        free_gb = shutil.disk_usage(paths.DATA_DIR.parent
                                    if paths.DATA_DIR.parent.exists()
                                    else Path.cwd()).free / 1e9
        # ~500 symbols x ~1300 daily bars is well under 100 MB, but the WAL and
        # a few months of journals want room.
        if free_gb > 2:
            self.add("disk", OK, f"{free_gb:.1f} GB free")
        else:
            self.add("disk", WARN, f"only {free_gb:.1f} GB free",
                     "the bar cache plus journals want a couple of GB")

        tz = os.environ.get("TZ") or time.tzname[0]
        if "Pacific" in str(tz) or "PDT" in str(tz) or "PST" in str(tz):
            self.add("timezone", OK, str(tz))
        else:
            self.add("timezone", WARN, f"{tz} — not Pacific",
                     "on the VPS run: timedatectl set-timezone America/Los_Angeles. "
                     "Otherwise the timers drift an hour twice a year.")

        for module in ("pandas", "numpy", "requests", "yaml"):
            try:
                __import__(module)
                self.add(f"import {module}", OK, "")
            except ImportError:
                self.add(f"import {module}", BLOCK, "missing",
                         "pip install -r requirements.txt")
        for module, why in (("alpaca", "market data"), ("anthropic", "Stage B")):
            try:
                __import__(module)
                self.add(f"import {module}", OK, "")
            except ImportError:
                self.add(f"import {module}", WARN, f"missing — needed for {why}",
                         "pip install -r requirements.txt")

    # -- 5. dry run ---------------------------------------------------------

    def check_pipeline(self) -> None:
        """Dry-run both stages in an ISOLATED data directory.

        This used to call `stage_a.run(offline=True)` in-process, which wrote
        synthetic candidates straight over the real `candidates.json`. Stage B
        would then brief you on SYN014 and SYN020 with a straight face, and the
        only clue was that the tickers were not real -- a check that silently
        destroys the thing it is checking is worse than no check.

        A subprocess with SCREENER_DATA pointed at a temp directory is the only
        version that cannot touch live state: the path is resolved at import
        time, so setting the variable in-process would not reliably take.
        """
        print("\nPIPELINE DRY RUN (offline, isolated)")
        import subprocess
        import tempfile

        with tempfile.TemporaryDirectory(prefix="preflight-") as sandbox:
            env = dict(os.environ,
                       SCREENER_DATA=sandbox,
                       SCREENER_JOURNAL=sandbox,
                       SCREENER_LOGS=sandbox)
            root = Path(__file__).resolve().parent.parent

            for label, module, args in (
                ("stage A", "pipeline.stage_a_nightly", ["--offline", "--max-symbols", "25"]),
                ("stage B", "pipeline.stage_b_brief", ["--offline", "--dry-run"]),
            ):
                try:
                    proc = subprocess.run(
                        [sys.executable, "-m", module, *args],
                        cwd=root, env=env, capture_output=True, text=True, timeout=300)
                except subprocess.TimeoutExpired:
                    self.add(label, BLOCK, "timed out after 300s")
                    return
                if proc.returncode == 0:
                    self.add(label, OK, _last_useful_line(proc.stdout, proc.stderr))
                else:
                    tail = (proc.stderr or proc.stdout).strip().splitlines()[-1:]
                    self.add(label, BLOCK, tail[0][:160] if tail else "failed",
                             "run ./run_tests.sh to localise it")
                    return

        self.add("live data untouched", OK, "dry run used a temp directory")

    # -- report -------------------------------------------------------------

    def summary(self) -> int:
        blocked = [c for c in self.checks if c.status == BLOCK]
        warned = [c for c in self.checks if c.status == WARN]

        print("\n" + "=" * 66)
        if blocked:
            print(f"NOT READY — {len(blocked)} blocking issue(s)")
            for c in blocked:
                print(f"  ✗ {c.name}: {c.detail}")
                if c.fix:
                    print(f"      {c.fix}")
        else:
            print("READY for a live run.")
        if warned:
            print(f"\n{len(warned)} warning(s) — the system runs, degraded:")
            for c in warned:
                print(f"  ! {c.name}: {c.detail}")
        print("=" * 66)

        if not blocked:
            print("\nNext:")
            print("  1. python3 -m pipeline.stage_a_nightly      # first live screen")
            print("  2. inspect data/candidates.json — do the names look sane?")
            print("  3. python3 -m pipeline.stage_b_brief --dry-run")
            print("  4. deploy to the VPS (docs/setup-vps.md), then let it run "
                  "a month\n     before a single real dollar goes in.")
        return 1 if blocked else 0


def _last_useful_line(stdout: str, stderr: str) -> str:
    """The most informative line a stage printed, for the one-line summary."""
    for stream in (stderr, stdout):
        for line in reversed((stream or "").strip().splitlines()):
            for marker in ("candidates:", "brief delivered:", "wrote "):
                if marker in line:
                    return line.split(marker, 1)[1].strip()[:90] if marker != "wrote " \
                        else line.strip()[-90:]
    return "completed"


def apply_volume_scale(scale: float) -> None:
    """Persist the measured scale so `AlpacaProvider` picks it up."""
    config = Path("config/screen.yaml")
    raw = config.read_text()
    if "volume_scale:" in raw:
        import re
        raw = re.sub(r"volume_scale:\s*[\d.]+", f"volume_scale: {scale}", raw)
    else:
        raw = raw.replace(
            "universe:",
            "universe:\n  # Measured by `python3 -m pipeline.preflight`. Alpaca's\n"
            "  # free IEX feed reports a fraction of consolidated volume; this\n"
            "  # multiplier brings it back onto the scale min_avg_volume assumes.\n"
            f"  volume_scale: {scale}", 1)
    config.write_text(raw)
    print(f"\nwrote volume_scale: {scale} to config/screen.yaml")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Pre-live-run checks")
    parser.add_argument("--fix-volume-scale", action="store_true",
                        help="write the measured scale into config/screen.yaml")
    parser.add_argument("--skip-telegram", action="store_true",
                        help="do not send a test message")
    parser.add_argument("--json", type=Path, default=None)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.ERROR, format="%(message)s")
    paths.ensure_dirs()
    paths.load_dotenv()

    print("=" * 66)
    print("PREFLIGHT — checking everything before the first live run")
    print("=" * 66)

    pf = Preflight()
    pf.check_env()
    pf.check_environment()
    pf.check_alpaca()
    scale = pf.check_volume_scale()
    pf.check_universe()
    pf.check_finnhub()
    if not args.skip_telegram:
        pf.check_telegram()
    pf.check_pipeline()

    if args.fix_volume_scale and scale and scale != 1.0:
        apply_volume_scale(scale)

    code = pf.summary()
    if args.json:
        args.json.write_text(json.dumps(
            [{"name": c.name, "status": c.status, "detail": c.detail,
              "fix": c.fix, **c.data} for c in pf.checks], indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
