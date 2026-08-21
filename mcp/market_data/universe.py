"""The S&P 500 constituent list.

Locked decision #1. ~500 liquid, well-covered names -- small enough that a full
refresh sits comfortably inside every free tier, clean enough that the screen's
output is worth reading.

    load_universe()                      # cached list, refreshed weekly
    load_universe(force_refresh=True)    # fetch now

SURVIVORSHIP WARNING, and it is not a footnote:

    This returns TODAY'S membership. Screening today, that is exactly right.
    Backtesting with it is not -- you would be running a 2019 screen over the
    companies that were successful enough to still be in the index in 2026,
    having silently deleted everyone who was dropped for underperforming.

    Published estimates of the resulting overstatement run to a few percentage
    points of annual return, which is comfortably larger than most edges this
    screen could plausibly find. So: use this for live screening, and for any
    backtest either source a point-in-time membership series or write the
    caveat into the results. `architecture.md` §10 rule 2 is the same point.
"""

from __future__ import annotations

import csv
import logging
import os
from datetime import date, timedelta
from pathlib import Path

log = logging.getLogger(__name__)

def _default_data_dir() -> Path:
    """Shared with pipeline.paths so there is exactly one default.

    NOTE: `Path("") or fallback` does NOT work -- Path("") is Path(".") which is
    truthy, so the fallback never fires and everything lands in the current
    working directory. Test the environment string itself.
    """
    from pipeline.paths import DATA_DIR as _shared
    return _shared


_env = os.environ.get("SCREENER_DATA", "").strip()
DATA_DIR = Path(_env) if _env else _default_data_dir()
CACHE_FILE = DATA_DIR / "universe" / "sp500.csv"
WIKIPEDIA_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
MAX_AGE_DAYS = 7

# Index membership changes a handful of times a year. If a fetch returns
# wildly the wrong number of names, something upstream changed shape and we
# should keep the known-good list rather than screen a broken universe.
MIN_PLAUSIBLE = 450
MAX_PLAUSIBLE = 560


class UniverseError(RuntimeError):
    pass


def load_universe(
    path: str | Path | None = None,
    *,
    force_refresh: bool = False,
    allow_fetch: bool = True,
) -> list[str]:
    """Symbols for the screen. Cached to CSV, refetched when older than a week."""
    cache = Path(path or CACHE_FILE)

    if not force_refresh and _fresh_enough(cache):
        return _read_cache(cache)

    last_error: str | None = None
    if allow_fetch:
        try:
            symbols, sectors = _fetch()
            _write_cache(cache, symbols, sectors)
            return symbols
        except Exception as exc:                    # noqa: BLE001
            last_error = f"{type(exc).__name__}: {exc}"
            log.warning("universe refresh failed (%s); falling back to cache", exc)

    if cache.exists():
        log.warning("using stale universe cache from %s", _cache_date(cache))
        return _read_cache(cache)

    raise UniverseError(
        f"no universe available and no cache at {cache}.\n"
        f"    underlying error -> {last_error or 'fetch not attempted'}\n"
        "    Run once with network access, or drop a CSV with symbol,sector "
        "columns at that path."
    )


def load_sectors(path: str | Path | None = None) -> dict[str, str]:
    """symbol -> GICS sector, for the sizing concentration cap."""
    cache = Path(path or CACHE_FILE)
    if not cache.exists():
        return {}
    with cache.open() as fh:
        return {row["symbol"]: row.get("sector", "") for row in csv.DictReader(fh)}


# --------------------------------------------------------------------------- #

def _fetch() -> tuple[list[str], dict[str, str]]:
    """Scrape the current constituent table.

    Wikipedia is the pragmatic free source and is well maintained, but it is a
    scrape: the table shape can change without warning, and the site rejects
    unfamiliar clients. Two sources are tried in order -- the rendered page,
    then the MediaWiki API, which is meant for automation and is the more
    reliable of the two under a firewall.

    The result is sanity-checked against MIN/MAX_PLAUSIBLE before it is allowed
    to overwrite a working cache.
    """
    errors = []
    for name, loader in (("page", _fetch_via_page), ("api", _fetch_via_api)):
        try:
            frame = loader()
        except Exception as exc:                      # noqa: BLE001
            errors.append(f"{name}: {type(exc).__name__}: {exc}")
            log.info("universe source %s failed: %s", name, exc)
            continue
        try:
            return _parse_table(frame)
        except Exception as exc:                      # noqa: BLE001
            errors.append(f"{name} parse: {exc}")
    raise UniverseError("; ".join(errors) or "no source succeeded")


def _headers() -> dict:
    # Wikipedia rejects urllib's default User-Agent with a bare 403, which
    # reads like a firewall problem and is not one. Any descriptive UA works.
    return {
        "User-Agent": os.environ.get(
            "SEC_USER_AGENT",
            "stock-recommender/1.0 (personal screener)"),
        "Accept": "text/html,application/xhtml+xml,application/json",
        "Accept-Language": "en-US,en;q=0.9",
    }


def _fetch_via_page():
    import io

    import pandas as pd
    import requests

    response = requests.get(WIKIPEDIA_URL, headers=_headers(), timeout=(10, 30))
    response.raise_for_status()
    return pd.read_html(io.StringIO(response.text))


def _fetch_via_api():
    """The MediaWiki parse API. Built for automation, so it 403s far less."""
    import io

    import pandas as pd
    import requests

    response = requests.get(
        "https://en.wikipedia.org/w/api.php",
        params={"action": "parse", "page": "List of S&P 500 companies",
                "format": "json", "prop": "text", "formatversion": "2"},
        headers=_headers(), timeout=(10, 30))
    response.raise_for_status()
    html = response.json()["parse"]["text"]
    return pd.read_html(io.StringIO(html))


def _parse_table(tables) -> tuple[list[str], dict[str, str]]:
    for table in tables:
        columns = {str(c).lower() for c in table.columns}
        if "symbol" in columns and any("sector" in c for c in columns):
            frame = table
            break
    else:
        raise UniverseError("no constituent table found in the response")

    frame.columns = [str(c).lower() for c in frame.columns]
    sector_col = next(c for c in frame.columns if "sector" in c)

    # Wikipedia uses BRK.B; vendors differ (BRK/B, BRK-B). Normalise to dots
    # inside the system and let each provider translate at its edge.
    symbols = [str(s).strip().upper().replace("-", ".") for s in frame["symbol"]]
    sectors = dict(zip(symbols, frame[sector_col].astype(str)))

    if not MIN_PLAUSIBLE <= len(symbols) <= MAX_PLAUSIBLE:
        raise UniverseError(
            f"got {len(symbols)} symbols, expected {MIN_PLAUSIBLE}-{MAX_PLAUSIBLE}; "
            "the source table probably changed shape")
    if len(set(symbols)) != len(symbols):
        raise UniverseError("the fetched universe contains duplicate symbols")

    return symbols, sectors


def _write_cache(cache: Path, symbols: list[str], sectors: dict[str, str]) -> None:
    cache.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache.with_suffix(".csv.tmp")
    with tmp.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["symbol", "sector", "as_of"])
        today = date.today().isoformat()
        for symbol in symbols:
            writer.writerow([symbol, sectors.get(symbol, ""), today])
    tmp.replace(cache)          # atomic: a crashed write cannot truncate the cache
    log.info("universe cache written: %d symbols", len(symbols))


def _read_cache(cache: Path) -> list[str]:
    with cache.open() as fh:
        symbols = [row["symbol"] for row in csv.DictReader(fh) if row.get("symbol")]
    if not symbols:
        raise UniverseError(f"universe cache at {cache} is empty")
    return symbols


def _cache_date(cache: Path) -> date | None:
    try:
        with cache.open() as fh:
            first = next(csv.DictReader(fh), None)
        return date.fromisoformat(first["as_of"]) if first and first.get("as_of") else None
    except Exception:                                # noqa: BLE001
        return None


def _fresh_enough(cache: Path) -> bool:
    if not cache.exists():
        return False
    stamped = _cache_date(cache)
    return stamped is not None and (date.today() - stamped) <= timedelta(days=MAX_AGE_DAYS)


def offline_universe(n: int = 60) -> list[str]:
    """Fake tickers for offline runs. Deterministic, obviously not real."""
    return [f"SYN{i:03d}" for i in range(n)]


# --------------------------------------------------------------------------- #

def bootstrap_from_csv(source: str | Path, cache: str | Path | None = None) -> int:
    """Import a symbol list you obtained by hand.

    Accepts any CSV with a `symbol` column (a `sector` column is used if
    present, and everything falls back to UNKNOWN if not). Exists so a
    firewall between you and Wikipedia is an inconvenience rather than a
    blocker -- any broker or index page will give you the list.
    """
    source = Path(source)
    with source.open() as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        raise UniverseError(f"{source} is empty")

    key = next((k for k in rows[0] if k and k.strip().lower() in
                ("symbol", "ticker", "symbols")), None)
    if key is None:
        raise UniverseError(
            f"{source} has no symbol column; found {list(rows[0])}")
    sector_key = next((k for k in rows[0] if k and "sector" in k.lower()), None)

    symbols, sectors = [], {}
    for row in rows:
        raw = (row.get(key) or "").strip().upper().replace("-", ".")
        if not raw or raw in sectors:
            continue
        symbols.append(raw)
        sectors[raw] = (row.get(sector_key) or "UNKNOWN").strip() if sector_key \
            else "UNKNOWN"

    if not symbols:
        raise UniverseError(f"no usable symbols in {source}")

    _write_cache(Path(cache or CACHE_FILE), symbols, sectors)
    return len(symbols)


def _main() -> int:
    """Diagnose the universe fetch.

        python -m mcp.market_data.universe

    Prints exactly which step fails: the HTML parser, the network, the table
    shape, or the cache write. `load_universe` swallows these into one message
    on purpose (it must not crash the 01:00 job over a Wikipedia hiccup), which
    makes a dedicated diagnostic worth having.
    """
    import argparse
    import logging
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")

    parser = argparse.ArgumentParser(description="Diagnose the universe fetch")
    parser.add_argument("--from-csv", type=Path, default=None,
                        help="import a symbol list instead of fetching")
    args = parser.parse_args()

    if args.from_csv:
        count = bootstrap_from_csv(args.from_csv)
        print(f"imported {count} symbols to {CACHE_FILE}")
        return 0

    print(f"cache path : {CACHE_FILE}")
    print(f"cached     : {'yes, ' + str(_cache_date(CACHE_FILE)) if CACHE_FILE.exists() else 'no'}")
    print(f"source     : {WIKIPEDIA_URL}\n")

    print("checking for an HTML parser...")
    for name in ("lxml", "bs4", "html5lib"):
        try:
            __import__(name)
            print(f"  found {name}")
        except ImportError:
            print(f"  missing {name}")

    print("\nfetching...")
    try:
        symbols, sectors = _fetch()
    except Exception as exc:                          # noqa: BLE001
        print(f"  FAILED: {type(exc).__name__}: {exc}")
        print("\nIf this is an ImportError or mentions a parser:")
        print("  pip install lxml beautifulsoup4 html5lib")
        print("\nIf it is a network error, you can supply the list by hand:")
        print("  1. copy the S&P 500 table from any source into a CSV with a")
        print("     'symbol' column (a 'sector' column is used if present)")
        print("  2. python -m mcp.market_data --from-csv path\\to\\list.csv")
        return 1

    print(f"  ok: {len(symbols)} symbols, {len(set(sectors.values()))} sectors")
    print(f"  first 10: {symbols[:10]}")
    _write_cache(CACHE_FILE, symbols, sectors)
    print(f"\ncached to {CACHE_FILE}")
    return 0

# Entry point lives in mcp/market_data/__main__.py -- see the note there.
