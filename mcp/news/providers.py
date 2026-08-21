"""News, earnings and filings providers.

Free stack: Finnhub for company news and the earnings calendar, SEC EDGAR for
filings. Marketaux is wired in as an optional sentiment source but is not
required -- its free tier returns 3 articles per call, which is headlines, not
depth.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Protocol

log = logging.getLogger(__name__)

FINNHUB_BASE = "https://finnhub.io/api/v1"
EDGAR_BASE = "https://data.sec.gov"
EDGAR_MIN_INTERVAL = 0.11          # SEC allows 10 req/s; stay under it


@dataclass
class NewsContext:
    """Everything the catalyst check needs about one symbol."""
    symbol: str
    headlines: list[dict] = field(default_factory=list)
    earnings_date: date | None = None
    filings: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def degraded(self) -> bool:
        return bool(self.errors)

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "headlines": self.headlines,
            "earnings_date": self.earnings_date.isoformat() if self.earnings_date else None,
            "filings": self.filings,
            "errors": self.errors,
        }


class NewsProvider(Protocol):
    name: str

    def context(self, symbol: str, *, as_of: date, lookback_days: int) -> NewsContext: ...


# --------------------------------------------------------------------------- #

class FinnhubProvider:
    """Company news + earnings calendar. Free tier: 60 requests/minute.

    Free-tier endpoint coverage has moved around historically, so every call
    degrades to an empty list plus a recorded error rather than raising. A
    missing news feed should make the brief say "no news available", not kill
    the morning.
    """

    name = "finnhub"

    def __init__(self, api_key: str | None = None, session=None):
        self.api_key = api_key or os.environ.get("FINNHUB_API_KEY", "")
        self._session = session

    @property
    def session(self):
        if self._session is None:
            import requests
            self._session = requests.Session()
        return self._session

    def _get(self, path: str, params: dict) -> list | dict | None:
        if not self.api_key:
            raise RuntimeError("FINNHUB_API_KEY is not set")
        params = {**params, "token": self.api_key}
        resp = self.session.get(f"{FINNHUB_BASE}{path}", params=params, timeout=(5, 20))
        if resp.status_code == 429:
            time.sleep(2)
            resp = self.session.get(f"{FINNHUB_BASE}{path}", params=params, timeout=(5, 20))
        resp.raise_for_status()
        return resp.json()

    def context(self, symbol: str, *, as_of: date, lookback_days: int = 7) -> NewsContext:
        ctx = NewsContext(symbol=symbol)
        start = as_of - timedelta(days=lookback_days)

        try:
            raw = self._get("/company-news", {"symbol": symbol,
                                              "from": start.isoformat(),
                                              "to": as_of.isoformat()}) or []
            ctx.headlines = [
                {"headline": item.get("headline", ""),
                 "summary": (item.get("summary") or "")[:400],
                 "source": item.get("source", ""),
                 "url": item.get("url", ""),
                 "datetime": _epoch_to_iso(item.get("datetime"))}
                for item in raw[:12]
            ]
        except Exception as exc:                          # noqa: BLE001
            ctx.errors.append(f"news: {type(exc).__name__}")
            log.warning("finnhub news failed for %s: %s", symbol, exc)

        try:
            # Look forward far enough to cover a multi-week swing hold.
            cal = self._get("/calendar/earnings",
                            {"symbol": symbol, "from": as_of.isoformat(),
                             "to": (as_of + timedelta(days=45)).isoformat()}) or {}
            entries = cal.get("earningsCalendar", []) if isinstance(cal, dict) else []
            dates = sorted(e["date"] for e in entries if e.get("date"))
            ctx.earnings_date = date.fromisoformat(dates[0]) if dates else None
        except Exception as exc:                          # noqa: BLE001
            ctx.errors.append(f"earnings: {type(exc).__name__}")
            log.warning("finnhub earnings failed for %s: %s", symbol, exc)

        return ctx


class EdgarProvider:
    """Recent SEC filings by form type.

    The SEC requires a declared User-Agent of the form "Name email@domain" or
    it blocks you outright -- this is the single most common reason an EDGAR
    integration silently returns nothing.

    Form types this cares about, and why:
      8-K    material event -- could be anything, needs reading
      S-1 / S-3 / 424B*     registration or takedown: dilution incoming
      SC 13D / SC 13E3      activist stake or going-private
      DEF 14A               proxy; merger votes live here
    """

    name = "edgar"
    WATCHED_FORMS = ("8-K", "S-1", "S-3", "424B", "SC 13D", "SC 13E3", "DEF 14A")

    def __init__(self, user_agent: str | None = None, session=None,
                 ticker_map: dict[str, str] | None = None):
        self.user_agent = user_agent or os.environ.get("SEC_USER_AGENT", "")
        self._session = session
        self._ticker_map = ticker_map
        self._last_call = 0.0

    @property
    def session(self):
        if self._session is None:
            import requests
            self._session = requests.Session()
            self._session.headers.update({
                "User-Agent": self.user_agent,
                "Accept-Encoding": "gzip, deflate",
            })
        return self._session

    def _pace(self) -> None:
        elapsed = time.monotonic() - self._last_call
        if elapsed < EDGAR_MIN_INTERVAL:
            time.sleep(EDGAR_MIN_INTERVAL - elapsed)
        self._last_call = time.monotonic()

    def cik_for(self, symbol: str) -> str | None:
        if self._ticker_map is None:
            self._pace()
            resp = self.session.get("https://www.sec.gov/files/company_tickers.json",
                                    timeout=(5, 30))
            resp.raise_for_status()
            self._ticker_map = {
                str(row["ticker"]).upper(): f"{int(row['cik_str']):010d}"
                for row in resp.json().values()
            }
        return self._ticker_map.get(symbol.upper())

    def recent_filings(self, symbol: str, *, as_of: date,
                       lookback_days: int = 30) -> list[dict]:
        if not self.user_agent or "@" not in self.user_agent:
            raise RuntimeError(
                "SEC_USER_AGENT must be set to 'Your Name your@email.com'. "
                "EDGAR blocks undeclared automated access."
            )
        cik = self.cik_for(symbol)
        if not cik:
            return []

        self._pace()
        resp = self.session.get(f"{EDGAR_BASE}/submissions/CIK{cik}.json", timeout=(5, 30))
        resp.raise_for_status()
        recent = resp.json().get("filings", {}).get("recent", {})

        cutoff = as_of - timedelta(days=lookback_days)
        out = []
        for form, filed, doc in zip(recent.get("form", []),
                                    recent.get("filingDate", []),
                                    recent.get("primaryDocDescription", [])):
            try:
                filed_on = date.fromisoformat(filed)
            except (TypeError, ValueError):
                continue
            if filed_on < cutoff or filed_on > as_of:
                continue
            if not any(form.startswith(w) for w in self.WATCHED_FORMS):
                continue
            out.append({"form": form, "filed": filed, "description": doc or ""})
        return out[:15]


class OfflineNewsProvider:
    """Deterministic fake news so Stage B runs without keys.

    Deliberately seeds a few disqualifying situations -- imminent earnings, a
    424B takedown, a reverse split -- so the disqualify path is exercised on
    every offline run rather than only when a live feed happens to produce one.
    """

    name = "offline"

    def __init__(self, seed: int = 5):
        self.seed = seed

    def context(self, symbol: str, *, as_of: date, lookback_days: int = 7) -> NewsContext:
        import random
        rng = random.Random(f"{symbol}:{self.seed}")
        ctx = NewsContext(symbol=symbol)

        templates = [
            ("{s} added to a widely tracked index", "Passive inflows expected."),
            ("{s} raises full-year guidance", "Management lifted the revenue range."),
            ("Analyst initiates {s} at Buy", "Price target implies upside."),
            ("{s} announces $500m buyback", "Board authorised a repurchase."),
            ("{s} names new CFO", "Internal promotion, effective next quarter."),
        ]
        for i in range(rng.randint(1, 4)):
            headline, summary = rng.choice(templates)
            ctx.headlines.append({
                "headline": headline.format(s=symbol),
                "summary": summary,
                "source": "SyntheticWire",
                "url": f"https://example.invalid/{symbol.lower()}/{i}",
                "datetime": (as_of - timedelta(days=rng.randint(0, lookback_days))).isoformat(),
            })

        roll = rng.random()
        if roll < 0.25:
            ctx.earnings_date = as_of + timedelta(days=rng.randint(1, 9))   # disqualifying
        elif roll < 0.5:
            ctx.earnings_date = as_of + timedelta(days=rng.randint(40, 80))

        if rng.random() < 0.15:
            ctx.filings.append({"form": "424B5", "filed": as_of.isoformat(),
                                "description": "Prospectus supplement"})
        if rng.random() < 0.08:
            ctx.headlines.append({
                "headline": f"{symbol} announces 1-for-10 reverse stock split",
                "summary": "Effective next month.", "source": "SyntheticWire",
                "url": "https://example.invalid/rs", "datetime": as_of.isoformat()})
        return ctx


# --------------------------------------------------------------------------- #

def resolve_news_provider(name: str | None = None, **kwargs):
    name = (name or os.environ.get("SCREENER_NEWS_PROVIDER", "finnhub")).lower()
    if name == "finnhub":
        return FinnhubProvider(**kwargs)
    if name == "offline":
        return OfflineNewsProvider(**kwargs)
    raise ValueError(f"unknown news provider {name!r}")


def get_context(
    symbols: list[str],
    *,
    as_of: date | None = None,
    lookback_days: int = 7,
    provider=None,
    edgar: EdgarProvider | None = None,
) -> dict[str, NewsContext]:
    """News, earnings and filings for each symbol.

    Only ever called with the ~20 screened candidates, never the full universe
    -- which is what keeps this inside Finnhub's 60/min and Marketaux's 100/day.
    """
    as_of = as_of or date.today()
    prov = provider or resolve_news_provider()
    out: dict[str, NewsContext] = {}

    for symbol in symbols:
        try:
            ctx = prov.context(symbol, as_of=as_of, lookback_days=lookback_days)
        except Exception as exc:                          # noqa: BLE001
            ctx = NewsContext(symbol=symbol, errors=[f"provider: {type(exc).__name__}"])
            log.warning("news context failed for %s: %s", symbol, exc)

        if edgar is not None:
            try:
                ctx.filings.extend(edgar.recent_filings(symbol, as_of=as_of))
            except Exception as exc:                      # noqa: BLE001
                ctx.errors.append(f"edgar: {type(exc).__name__}")
                log.warning("edgar failed for %s: %s", symbol, exc)

        out[symbol] = ctx
    return out


def _epoch_to_iso(value) -> str:
    from datetime import datetime, timezone
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc).date().isoformat()
    except (TypeError, ValueError, OSError):
        return ""
