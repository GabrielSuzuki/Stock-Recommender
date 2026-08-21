"""SQLite bar cache with incremental refresh.

The screen needs ~500 symbols x 500 daily bars. Pulling that fresh every night
is 250k rows over the wire for maybe 500 rows of new information, and it is the
fastest way to exhaust a free tier. So: pull history once, then fetch only the
delta.

SQLite rather than Parquet because the access pattern is "give me one symbol's
bars since date X", which is an index lookup, not a columnar scan. It also
gives transactional writes for free, so a crashed refresh cannot leave a
half-written symbol behind.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

SCHEMA = """
CREATE TABLE IF NOT EXISTS bars (
    symbol TEXT    NOT NULL,
    date   TEXT    NOT NULL,          -- ISO date, UTC session date
    open   REAL    NOT NULL,
    high   REAL    NOT NULL,
    low    REAL    NOT NULL,
    close  REAL    NOT NULL,
    volume REAL    NOT NULL,
    PRIMARY KEY (symbol, date)
);
CREATE INDEX IF NOT EXISTS idx_bars_symbol_date ON bars(symbol, date);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class BarCache:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn:
            conn.executescript(SCHEMA)
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    # -- reads --------------------------------------------------------------

    def last_date(self, symbol: str) -> date | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT MAX(date) FROM bars WHERE symbol = ?", (symbol,)
            ).fetchone()
        return date.fromisoformat(row[0]) if row and row[0] else None

    def last_dates(self, symbols: list[str]) -> dict[str, date]:
        """One query for the whole universe. 500 individual queries is 500 round
        trips to disk for information that fits in a single GROUP BY."""
        if not symbols:
            return {}
        placeholders = ",".join("?" * len(symbols))
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"SELECT symbol, MAX(date) FROM bars WHERE symbol IN ({placeholders}) "
                "GROUP BY symbol", symbols
            ).fetchall()
        return {s: date.fromisoformat(d) for s, d in rows if d}

    def read(self, symbol: str, start: date | None = None) -> pd.DataFrame:
        query = "SELECT date, open, high, low, close, volume FROM bars WHERE symbol = ?"
        params: list = [symbol]
        if start is not None:
            query += " AND date >= ?"
            params.append(start.isoformat())
        query += " ORDER BY date"
        with closing(self._connect()) as conn:
            frame = pd.read_sql_query(query, conn, params=params)
        if frame.empty:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"],
                                index=pd.DatetimeIndex([], name="date"))
        frame["date"] = pd.to_datetime(frame["date"])
        return frame.set_index("date")

    def read_many(self, symbols: list[str], start: date | None = None
                  ) -> dict[str, pd.DataFrame]:
        return {s: self.read(s, start) for s in symbols}

    # -- writes -------------------------------------------------------------

    def upsert(self, symbol: str, bars: pd.DataFrame) -> int:
        """Insert or replace. Returns rows written.

        REPLACE rather than INSERT OR IGNORE: when a provider restates a bar
        (splits, late corrections) we want the new value, not the stale one.
        """
        if bars.empty:
            return 0
        records = [
            (symbol, idx.date().isoformat(), float(r.open), float(r.high),
             float(r.low), float(r.close), float(r.volume))
            for idx, r in bars.iterrows()
        ]
        with closing(self._connect()) as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO bars "
                "(symbol, date, open, high, low, close, volume) VALUES (?,?,?,?,?,?,?)",
                records,
            )
            conn.commit()
        return len(records)

    def drop_symbol(self, symbol: str) -> None:
        """Used when a split is detected -- the whole history must be refetched
        because every bar before the split is now wrong."""
        with closing(self._connect()) as conn:
            conn.execute("DELETE FROM bars WHERE symbol = ?", (symbol,))
            conn.commit()

    # -- metadata -----------------------------------------------------------

    def set_meta(self, key: str, value: str) -> None:
        with closing(self._connect()) as conn:
            conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)",
                         (key, value))
            conn.commit()

    def get_meta(self, key: str) -> str | None:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def stats(self) -> dict:
        with closing(self._connect()) as conn:
            symbols, rows, first, last = conn.execute(
                "SELECT COUNT(DISTINCT symbol), COUNT(*), MIN(date), MAX(date) FROM bars"
            ).fetchone()
        return {"symbols": symbols or 0, "rows": rows or 0,
                "first_date": first, "last_date": last,
                "size_mb": round(self.path.stat().st_size / 1e6, 1)
                if self.path.exists() else 0.0}


def detect_split(cached_tail: pd.DataFrame, fresh_head: pd.DataFrame,
                 tolerance: float = 0.15) -> bool:
    """True when cached and freshly-fetched bars disagree about the same date.

    Adjusted-price feeds restate the whole history on a split. If we only ever
    append, the cache keeps pre-split prices forever and every moving average
    silently becomes wrong -- the screen would fire on a phantom breakout. So
    we re-request one overlapping bar each refresh and compare.

    A 15% tolerance ignores ordinary restatements while catching any real split
    (the smallest common one is 3:2, a 33% move).
    """
    if cached_tail.empty or fresh_head.empty:
        return False
    shared = cached_tail.index.intersection(fresh_head.index)
    if shared.empty:
        return False
    old = cached_tail.loc[shared, "close"]
    new = fresh_head.loc[shared, "close"]
    ratio = (new / old).replace([float("inf"), -float("inf")], float("nan")).dropna()
    if ratio.empty:
        return False
    return bool(((ratio - 1.0).abs() > tolerance).any())


def default_start(lookback_days: int, today: date | None = None) -> date:
    """Calendar days to request for a target number of trading days.

    Roughly 252 trading days per 365 calendar days, plus a margin for holidays
    so a 500-bar request does not come back 12 bars short in a year with an
    unusual holiday calendar.
    """
    today = today or date.today()
    return today - timedelta(days=int(lookback_days * 365 / 252) + 15)
