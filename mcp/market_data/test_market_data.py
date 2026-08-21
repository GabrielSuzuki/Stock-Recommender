"""Cache, split detection, staleness and provider-facade tests. No network."""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from mcp.market_data.cache import (BarCache, default_start,  # noqa: E402
                                   detect_split)
from mcp.market_data.provider import (BarBundle, StaleDataError,  # noqa: E402
                                      _previous_business_day, get_bars)


def bars(start: str, n: int, price: float = 100.0, step: float = 1.0) -> pd.DataFrame:
    idx = pd.bdate_range(start=start, periods=n)
    closes = [price + i * step for i in range(n)]
    return pd.DataFrame(
        {"open": closes, "high": [c * 1.01 for c in closes],
         "low": [c * 0.99 for c in closes], "close": closes,
         "volume": [1_000_000.0] * n}, index=idx)


class TestCache(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.cache = BarCache(Path(self.dir.name) / "bars.sqlite")

    def tearDown(self):
        self.dir.cleanup()

    def test_roundtrip(self):
        written = self.cache.upsert("AAA", bars("2026-01-05", 10))
        self.assertEqual(written, 10)
        read = self.cache.read("AAA")
        self.assertEqual(len(read), 10)
        self.assertListEqual(list(read.columns),
                             ["open", "high", "low", "close", "volume"])

    def test_read_empty_symbol_has_right_shape(self):
        empty = self.cache.read("NOPE")
        self.assertTrue(empty.empty)
        self.assertIn("close", empty.columns)

    def test_upsert_replaces_not_duplicates(self):
        self.cache.upsert("AAA", bars("2026-01-05", 5, price=100))
        self.cache.upsert("AAA", bars("2026-01-05", 5, price=200))
        read = self.cache.read("AAA")
        self.assertEqual(len(read), 5)
        self.assertAlmostEqual(read["close"].iloc[0], 200.0)   # restatement wins

    def test_last_date(self):
        frame = bars("2026-01-05", 10)
        self.cache.upsert("AAA", frame)
        self.assertEqual(self.cache.last_date("AAA"), frame.index[-1].date())
        self.assertIsNone(self.cache.last_date("NOPE"))

    def test_last_dates_batch(self):
        self.cache.upsert("AAA", bars("2026-01-05", 5))
        self.cache.upsert("BBB", bars("2026-01-05", 8))
        out = self.cache.last_dates(["AAA", "BBB", "CCC"])
        self.assertEqual(set(out), {"AAA", "BBB"})

    def test_read_with_start_filters(self):
        frame = bars("2026-01-05", 20)
        self.cache.upsert("AAA", frame)
        cut = frame.index[10].date()
        self.assertEqual(len(self.cache.read("AAA", start=cut)), 10)

    def test_drop_symbol(self):
        self.cache.upsert("AAA", bars("2026-01-05", 5))
        self.cache.drop_symbol("AAA")
        self.assertTrue(self.cache.read("AAA").empty)

    def test_meta(self):
        self.cache.set_meta("last_refresh", "2026-08-19")
        self.assertEqual(self.cache.get_meta("last_refresh"), "2026-08-19")
        self.assertIsNone(self.cache.get_meta("nope"))

    def test_stats(self):
        self.cache.upsert("AAA", bars("2026-01-05", 5))
        self.cache.upsert("BBB", bars("2026-01-05", 5))
        stats = self.cache.stats()
        self.assertEqual(stats["symbols"], 2)
        self.assertEqual(stats["rows"], 10)

    def test_empty_upsert_is_a_noop(self):
        self.assertEqual(self.cache.upsert("AAA", pd.DataFrame()), 0)


class TestSplitDetection(unittest.TestCase):
    def test_detects_2_for_1(self):
        cached = bars("2026-01-05", 5, price=400)
        fresh = bars("2026-01-05", 5, price=200, step=0.5)
        self.assertTrue(detect_split(cached, fresh))

    def test_detects_3_for_2(self):
        cached = bars("2026-01-05", 5, price=150, step=0)
        fresh = bars("2026-01-05", 5, price=100, step=0)
        self.assertTrue(detect_split(cached, fresh))

    def test_ignores_small_restatements(self):
        cached = bars("2026-01-05", 5, price=100, step=0)
        fresh = bars("2026-01-05", 5, price=100.5, step=0)
        self.assertFalse(detect_split(cached, fresh))

    def test_no_overlap_is_not_a_split(self):
        cached = bars("2026-01-05", 5)
        fresh = bars("2026-06-01", 5)
        self.assertFalse(detect_split(cached, fresh))

    def test_empty_inputs(self):
        self.assertFalse(detect_split(pd.DataFrame(), bars("2026-01-05", 3)))
        self.assertFalse(detect_split(bars("2026-01-05", 3), pd.DataFrame()))


class TestDateHelpers(unittest.TestCase):
    def test_previous_business_day_from_monday_is_friday(self):
        monday = date(2026, 8, 17)
        self.assertEqual(_previous_business_day(monday), date(2026, 8, 14))

    def test_previous_business_day_from_sunday_is_friday(self):
        self.assertEqual(_previous_business_day(date(2026, 8, 16)), date(2026, 8, 14))

    def test_previous_business_day_midweek(self):
        self.assertEqual(_previous_business_day(date(2026, 8, 20)), date(2026, 8, 19))

    def test_default_start_covers_enough_calendar_days(self):
        today = date(2026, 8, 20)
        start = default_start(500, today)
        # 500 trading days needs ~724 calendar days plus a holiday margin
        self.assertGreaterEqual((today - start).days, 724)


class FakeProvider:
    """Records calls so the incremental-refresh logic can be asserted on."""
    name = "fake"

    def __init__(self, frames: dict[str, pd.DataFrame], fail: set[str] | None = None):
        self.frames, self.fail, self.calls = frames, fail or set(), []

    def fetch(self, symbol, start, end):
        self.calls.append((symbol, start, end))
        if symbol in self.fail:
            raise RuntimeError("provider exploded")
        frame = self.frames.get(symbol, pd.DataFrame())
        return frame.loc[frame.index >= pd.Timestamp(start)] if not frame.empty else frame


class TestGetBars(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "bars.sqlite"
        self.today = date(2026, 8, 20)
        self.frames = {s: bars("2025-01-06", 420) for s in ("AAA", "BBB")}
        # make the last bar recent enough to pass the staleness check
        for s, f in self.frames.items():
            f.index = pd.bdate_range(end=pd.Timestamp(self.today), periods=len(f))

    def tearDown(self):
        self.dir.cleanup()

    def test_returns_a_bundle_not_a_dict_with_a_magic_key(self):
        out = get_bars(["AAA"], provider=FakeProvider(self.frames),
                       cache_path=self.path, as_of=self.today, rate_limit_per_min=0)
        self.assertIsInstance(out, BarBundle)
        self.assertEqual(set(out.bars), {"AAA"})
        self.assertNotIn("__stats__", out.bars)

    def test_second_run_makes_no_requests(self):
        prov = FakeProvider(self.frames)
        get_bars(["AAA", "BBB"], provider=prov, cache_path=self.path,
                 as_of=self.today, rate_limit_per_min=0)
        first = len(prov.calls)
        get_bars(["AAA", "BBB"], provider=prov, cache_path=self.path,
                 as_of=self.today, rate_limit_per_min=0)
        self.assertEqual(len(prov.calls), first, "cache did not prevent refetch")

    def test_one_bad_symbol_does_not_sink_the_run(self):
        prov = FakeProvider(self.frames, fail={"BBB"})
        out = get_bars(["AAA", "BBB"], provider=prov, cache_path=self.path,
                       as_of=self.today, rate_limit_per_min=0)
        self.assertEqual(set(out.bars), {"AAA"})
        self.assertIn("BBB", out.failed)
        self.assertEqual(out.stats["errors"], 1)

    def test_no_refresh_when_cache_holds_the_last_session(self):
        """Running Friday morning with Thursday's close cached is current.

        Stage A runs at 01:00, so the newest complete session is always the
        previous business day. Treating that as stale would re-request the
        whole universe every single morning for nothing.
        """
        prov = FakeProvider(self.frames)
        get_bars(["AAA"], provider=prov, cache_path=self.path,
                 as_of=self.today, rate_limit_per_min=0)
        prov.calls.clear()
        get_bars(["AAA"], provider=prov, cache_path=self.path,
                 as_of=self.today + timedelta(days=1), rate_limit_per_min=0)
        self.assertEqual(prov.calls, [])

    def test_stale_data_raises_rather_than_warns(self):
        old = {"AAA": bars("2024-01-08", 400)}
        with self.assertRaisesRegex(StaleDataError, "Refusing to screen"):
            get_bars(["AAA"], provider=FakeProvider(old), cache_path=self.path,
                     as_of=self.today, rate_limit_per_min=0)

    def test_no_data_at_all_raises(self):
        with self.assertRaisesRegex(StaleDataError, "no bars"):
            get_bars(["ZZZ"], provider=FakeProvider({}), cache_path=self.path,
                     as_of=self.today, rate_limit_per_min=0)

    def test_refresh_requests_overlap_for_split_detection(self):
        prov = FakeProvider(self.frames)
        get_bars(["AAA"], provider=prov, cache_path=self.path,
                 as_of=self.today, rate_limit_per_min=0)
        prov.calls.clear()
        # Aug 20 2026 is a Thursday. +1 day is Friday, whose previous business
        # day is Thursday -- still current, so no refresh is due and that is
        # correct. Jump to the following Monday so a new session exists.
        get_bars(["AAA"], provider=prov, cache_path=self.path,
                 as_of=self.today + timedelta(days=4), rate_limit_per_min=0)
        self.assertTrue(prov.calls, "expected a refresh request")
        _, start, _ = prov.calls[0]
        last_cached = self.frames["AAA"].index[-1].date()
        self.assertLess(start, last_cached, "refresh must overlap cached bars")


if __name__ == "__main__":
    unittest.main(verbosity=1)
