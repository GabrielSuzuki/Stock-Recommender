"""Bar providers. Add a new vendor here and nothing else changes.

Every provider implements `fetch(symbol, start, end) -> DataFrame` with the
canonical OHLCV shape. `get_bars()` wraps whichever one is configured with the
cache, the split check, and the staleness assertion.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import date, timedelta
from pathlib import Path
from dataclasses import dataclass, field
from typing import Protocol

import pandas as pd

from .cache import BarCache, default_start, detect_split

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
DEFAULT_CACHE = DATA_DIR / "bars.sqlite"

CANONICAL_COLUMNS = ["open", "high", "low", "close", "volume"]


class Provider(Protocol):
    name: str

    def fetch(self, symbol: str, start: date, end: date) -> pd.DataFrame: ...


# --------------------------------------------------------------------------- #
# Alpaca
# --------------------------------------------------------------------------- #

class AlpacaProvider:
    """Daily adjusted bars from Alpaca.

    Free tier: 200 requests/min, 7+ years of history, IEX feed only.

    The IEX-only caveat matters for one field: volume. IEX is a single venue
    with a low-single-digit share of consolidated tape, so `volume` here is a
    fraction of true traded volume -- typically 2-5%, and it varies by symbol.
    The 400k-share liquidity floor in config/screen.yaml is calibrated for
    consolidated volume, so on this feed it will reject almost everything
    unless scaled. `volume_scale` exists for that; measure it against a handful
    of known-liquid names before trusting the gate. This is the single most
    likely thing to be wrong on first contact with real data.
    """

    name = "alpaca"

    def __init__(self, api_key: str | None = None, api_secret: str | None = None,
                 feed: str = "iex", volume_scale: float = 1.0, client=None):
        self.api_key = api_key or os.environ.get("ALPACA_API_KEY_ID", "")
        self.api_secret = api_secret or os.environ.get("ALPACA_API_SECRET_KEY", "")
        self.feed = feed
        self.volume_scale = volume_scale
        self._client = client
        if client is None and not (self.api_key and self.api_secret):
            raise RuntimeError(
                "ALPACA_API_KEY_ID and ALPACA_API_SECRET_KEY must be set. "
                "A free paper account at alpaca.markets provides both."
            )

    @property
    def client(self):
        if self._client is None:
            try:
                from alpaca.data.historical import StockHistoricalDataClient
            except ImportError as exc:
                raise RuntimeError(
                    "alpaca-py is not installed. `pip install alpaca-py`."
                ) from exc
            self._client = StockHistoricalDataClient(self.api_key, self.api_secret)
        return self._client

    def fetch(self, symbol: str, start: date, end: date) -> pd.DataFrame:
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame

        request = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame.Day,
            start=pd.Timestamp(start),
            end=pd.Timestamp(end),
            feed=self.feed,
            adjustment="all",       # splits AND dividends; anything else is wrong
        )
        raw = self.client.get_stock_bars(request)
        frame = raw.df
        if frame is None or frame.empty:
            return _empty_bars()
        if isinstance(frame.index, pd.MultiIndex):
            frame = frame.xs(symbol, level="symbol")
        frame = frame.rename(columns=str.lower)
        missing = [c for c in CANONICAL_COLUMNS if c not in frame.columns]
        if missing:
            raise ValueError(f"alpaca returned {symbol} without {missing}")
        frame = frame[CANONICAL_COLUMNS].copy()
        frame.index = pd.to_datetime(frame.index).tz_localize(None).normalize()
        frame.index.name = "date"
        if self.volume_scale != 1.0:
            frame["volume"] = frame["volume"] * self.volume_scale
        return frame[~frame.index.duplicated(keep="last")].sort_index()


# --------------------------------------------------------------------------- #
# Offline
# --------------------------------------------------------------------------- #

class OfflineProvider:
    """Deterministic synthetic bars. No network, no keys.

    Exists so the whole pipeline can be exercised end to end -- including the
    cache, the screen, sizing and the JSON write -- before anyone has an API
    key, and so CI can run the nightly job without credentials.
    """

    name = "offline"

    def __init__(self, seed: int = 11):
        self.seed = seed

    def fetch(self, symbol: str, start: date, end: date) -> pd.DataFrame:
        import numpy as np

        from core import synthetic as syn

        rng = np.random.default_rng(abs(hash((symbol, self.seed))) % (2**32))
        days = max((end - start).days * 252 // 365, 260)
        growth = float(rng.uniform(-0.35, 0.95))
        bars = syn.stage2(
            n=days, start=float(rng.uniform(12, 220)), annual_growth=growth,
            noise=0.006, seed=int(rng.integers(1e6)),
            volume=float(rng.uniform(3e5, 8e6)), end=end.isoformat(),
        ) if growth > 0 else syn.downtrend(
            n=days, start=float(rng.uniform(20, 150)),
            annual_decline=growth, volume=float(rng.uniform(3e5, 8e6)),
            end=end.isoformat(),
        )
        return bars.loc[bars.index >= pd.Timestamp(start)]


def _empty_bars() -> pd.DataFrame:
    return pd.DataFrame(columns=CANONICAL_COLUMNS,
                        index=pd.DatetimeIndex([], name="date"))


def _configured_volume_scale() -> float:
    """Read `universe.volume_scale` from config/screen.yaml.

    Written by `python3 -m pipeline.preflight --fix-volume-scale` after it
    measures the IEX feed against consolidated reference volumes. Reading it
    here is what makes that measurement actually take effect -- an earlier
    version wrote the value and nothing consumed it, which is the worst kind of
    fix: one that looks applied and does nothing.
    """
    try:
        import yaml

        from core.screen import DEFAULT_CONFIG
        raw = yaml.safe_load(Path(DEFAULT_CONFIG).read_text()) or {}
        return float(raw.get("universe", {}).get("volume_scale", 1.0))
    except Exception:                                    # noqa: BLE001
        return 1.0


def resolve_provider(name: str | None = None, **kwargs) -> Provider:
    name = (name or os.environ.get("SCREENER_PROVIDER", "alpaca")).lower()
    if name == "alpaca":
        kwargs.setdefault("volume_scale", _configured_volume_scale())
        return AlpacaProvider(**kwargs)
    if name == "offline":
        return OfflineProvider(**kwargs)
    raise ValueError(f"unknown provider {name!r}; expected 'alpaca' or 'offline'")


# --------------------------------------------------------------------------- #
# the facade everything else uses
# --------------------------------------------------------------------------- #

@dataclass
class BarBundle:
    """Bars plus what happened while fetching them.

    A plain dict would force callers to remember a magic "__stats__" key, and
    the one who forgets writes a screen that silently ignores 40 failed
    symbols. Making the stats a separate field means the refresh report cannot
    be mistaken for a ticker.
    """
    bars: dict[str, pd.DataFrame] = field(default_factory=dict)
    stats: dict = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.bars)

    def __iter__(self):
        return iter(self.bars)

    def items(self):
        return self.bars.items()

    @property
    def failed(self) -> dict[str, str]:
        return self.stats.get("failures", {})


class StaleDataError(RuntimeError):
    """The cache does not contain a recent enough bar to screen on.

    Raised rather than warned. Screening on last week's prices produces a brief
    that looks completely normal and is completely wrong, which is the worst
    available failure mode.
    """


def get_bars(
    symbols: list[str],
    *,
    lookback_days: int = 500,
    provider: Provider | None = None,
    cache_path: str | Path | None = None,
    as_of: date | None = None,
    max_staleness_days: int = 5,
    rate_limit_per_min: int = 180,
    on_progress=None,
) -> BarBundle:
    """Return cached bars for `symbols`, refreshing only what is missing.

    Refresh logic per symbol:
      - nothing cached      -> full history from `lookback_days` back
      - cached through D    -> request from D-5 (overlap for the split check)
      - already current     -> no request at all

    The five-bar overlap is what makes split detection possible; without it we
    would only ever see new bars and could never notice that the old ones had
    been restated.
    """
    today = as_of or date.today()
    prov = provider or resolve_provider()
    cache = BarCache(cache_path or DEFAULT_CACHE)
    start_floor = default_start(lookback_days, today)

    known = cache.last_dates(symbols)
    fetched = refreshed = splits = errors = 0
    interval = 60.0 / rate_limit_per_min if rate_limit_per_min else 0.0
    failures: dict[str, str] = {}

    for i, symbol in enumerate(symbols):
        last = known.get(symbol)
        if last is not None and last >= _previous_business_day(today):
            continue

        start = start_floor if last is None else max(start_floor, last - timedelta(days=5))
        try:
            fresh = prov.fetch(symbol, start, today)
        except Exception as exc:                     # noqa: BLE001
            # One bad symbol must not take down a 500-name refresh. Collect and
            # report; the staleness check downstream decides if it was fatal.
            errors += 1
            failures[symbol] = f"{type(exc).__name__}: {exc}"
            log.warning("fetch failed for %s: %s", symbol, exc)
            continue

        if not fresh.empty:
            if last is not None:
                overlap = cache.read(symbol, start=start)
                if detect_split(overlap, fresh):
                    log.info("%s: split/restatement detected, refetching full history", symbol)
                    cache.drop_symbol(symbol)
                    try:
                        fresh = prov.fetch(symbol, start_floor, today)
                        splits += 1
                    except Exception as exc:         # noqa: BLE001
                        errors += 1
                        failures[symbol] = f"split refetch failed: {exc}"
                        continue
            cache.upsert(symbol, fresh)
            refreshed += 1
        fetched += 1

        if on_progress and i % 25 == 0:
            on_progress(i + 1, len(symbols))
        if interval:
            time.sleep(interval)

    cache.set_meta("last_refresh", today.isoformat())

    bars = cache.read_many(symbols, start=start_floor)
    bars = {s: b for s, b in bars.items() if not b.empty}

    _assert_fresh(bars, today, max_staleness_days)

    stats = {"requested": len(symbols), "fetched": fetched, "refreshed": refreshed,
             "splits_detected": splits, "errors": errors, "returned": len(bars),
             "failures": failures}
    log.info("bar refresh: %s", {k: v for k, v in stats.items() if k != "failures"})
    return BarBundle(bars=bars, stats=stats)


def _previous_business_day(today: date) -> date:
    """The most recent weekday strictly before `today`.

    Deliberately ignores market holidays: treating a holiday as "stale" costs
    one unnecessary API call, while treating a real gap as fresh would let the
    screen run on old prices. Wrong in the cheap direction.
    """
    d = today - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def _assert_fresh(bars: dict[str, pd.DataFrame], today: date, max_days: int) -> None:
    if not bars:
        raise StaleDataError("no bars available for any requested symbol")
    latest = max(frame.index[-1].date() for frame in bars.values() if len(frame))
    age = (today - latest).days
    if age > max_days:
        raise StaleDataError(
            f"most recent bar is {latest} ({age} days old, limit {max_days}). "
            "Refusing to screen on stale prices."
        )
