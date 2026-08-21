"""Paper account and broker tests."""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.exits import ExitConfig                              # noqa: E402
from paper.account import Account                              # noqa: E402
from paper.broker import ExecutionConfig, PaperBroker          # noqa: E402
from paper.run import deposit_dates                            # noqa: E402

MON = date(2026, 8, 3)


class TestDeposits(unittest.TestCase):
    def test_weekly_schedule_lands_on_mondays(self):
        dates = deposit_dates(date(2026, 8, 5), weeks=4)    # a Wednesday
        self.assertEqual(len(dates), 4)
        for d in dates:
            self.assertEqual(d.weekday(), 0)
        self.assertEqual(dates[0], date(2026, 8, 3))

    def test_cash_carries_over(self):
        """The whole point of four weeks: week 4 can buy what week 1 could not."""
        account = Account()
        for week in range(4):
            account.deposit(100.0, MON)
        self.assertAlmostEqual(account.cash, 400.0)
        self.assertAlmostEqual(account.deposited, 400.0)

    def test_rejects_non_positive_deposit(self):
        with self.assertRaises(ValueError):
            Account().deposit(0.0, MON)


class TestAccounting(unittest.TestCase):
    def setUp(self):
        self.account = Account(allow_fractional=True)
        self.account.deposit(400.0, MON)

    def test_buy_reduces_cash_and_opens_a_position(self):
        self.account.buy("AAA", 2.0, 50.0, MON, stop=45.0, target=65.0)
        self.assertAlmostEqual(self.account.cash, 300.0)
        self.assertIn("AAA", self.account.positions)

    def test_fractional_shares(self):
        self.account.buy("AAA", 0.25, 400.0, MON, stop=380.0, target=440.0)
        self.assertAlmostEqual(self.account.positions["AAA"].shares, 0.25)
        self.assertAlmostEqual(self.account.cash, 300.0)

    def test_whole_share_mode_truncates(self):
        account = Account(allow_fractional=False)
        account.deposit(400.0, MON)
        account.buy("AAA", 2.7, 50.0, MON, stop=45.0, target=65.0)
        self.assertAlmostEqual(account.positions["AAA"].shares, 2.0)

    def test_unaffordable_order_is_rejected_not_filled_on_margin(self):
        fill = self.account.buy("AAA", 100.0, 50.0, MON, stop=45.0, target=65.0)
        self.assertIsNone(fill)
        self.assertAlmostEqual(self.account.cash, 400.0)
        self.assertEqual(len(self.account.rejected), 1)
        self.assertEqual(self.account.rejected[0]["reason"], "cash")

    def test_cash_never_goes_negative(self):
        for _ in range(20):
            self.account.buy("AAA", 1.0, 50.0, MON, stop=45.0, target=65.0)
        self.assertGreaterEqual(self.account.cash, -1e-9)

    def test_sell_returns_cash_and_records_the_trade(self):
        self.account.buy("AAA", 2.0, 50.0, MON, stop=45.0, target=65.0)
        self.account.sell("AAA", 60.0, date(2026, 8, 10), reason="target")
        self.assertAlmostEqual(self.account.cash, 420.0)
        self.assertEqual(len(self.account.closed), 1)
        self.assertAlmostEqual(self.account.closed[0].pnl, 20.0)
        self.assertNotIn("AAA", self.account.positions)

    def test_partial_sell_keeps_the_rest_and_flags_scaled_out(self):
        self.account.buy("AAA", 2.0, 50.0, MON, stop=45.0, target=65.0)
        self.account.sell("AAA", 60.0, date(2026, 8, 10), fraction=0.5, reason="target")
        self.assertAlmostEqual(self.account.positions["AAA"].shares, 1.0)
        self.assertTrue(self.account.positions["AAA"].scaled_out)

    def test_r_multiple_on_a_closed_trade(self):
        # entry 50, stop 45 -> risk 5/share. Exit 60 -> +2R.
        self.account.buy("AAA", 2.0, 50.0, MON, stop=45.0, target=65.0)
        self.account.sell("AAA", 60.0, date(2026, 8, 10), reason="target")
        self.assertAlmostEqual(self.account.closed[0].r_multiple, 2.0)

    def test_profit_is_measured_against_deposits(self):
        """A raw equity curve rises simply because you kept paying in."""
        self.account.buy("AAA", 2.0, 50.0, MON, stop=45.0, target=65.0)
        prices = {"AAA": 60.0}
        self.assertAlmostEqual(self.account.equity(prices), 420.0)
        self.assertAlmostEqual(self.account.profit(prices), 20.0)
        self.assertAlmostEqual(self.account.return_pct(prices), 5.0)

    def test_unquoted_position_is_marked_at_cost_not_zero(self):
        """Marking an unquoted holding to zero would show a fake drawdown."""
        self.account.buy("AAA", 2.0, 50.0, MON, stop=45.0, target=65.0)
        self.assertAlmostEqual(self.account.equity({}), 400.0)

    def test_sell_unknown_symbol_is_a_noop(self):
        self.assertIsNone(self.account.sell("NOPE", 10.0, MON))


class TestPersistence(unittest.TestCase):
    def test_roundtrip(self):
        account = Account(allow_fractional=True)
        account.deposit(200.0, MON)
        account.buy("AAA", 1.5, 50.0, MON, stop=45.0, target=65.0, sector="Tech")
        account.sell("AAA", 60.0, date(2026, 8, 10), fraction=0.5, reason="target")
        account.mark(date(2026, 8, 10), {"AAA": 60.0})

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "acct.json"
            account.save(path)
            loaded = Account.load(path)

        self.assertAlmostEqual(loaded.cash, account.cash)
        self.assertEqual(len(loaded.positions), 1)
        self.assertEqual(len(loaded.closed), 1)
        self.assertAlmostEqual(loaded.closed[0].r_multiple, account.closed[0].r_multiple)
        self.assertEqual(len(loaded.equity_history), 1)

    def test_missing_file_returns_a_fresh_account(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(Account.load(Path(tmp) / "nope.json").cash, 0.0)

    def test_save_is_atomic(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "acct.json"
            Account().save(path)
            self.assertFalse(path.with_suffix(".tmp").exists())


class TestBroker(unittest.TestCase):
    def setUp(self):
        self.account = Account(allow_fractional=True)
        self.account.deposit(1000.0, MON)
        self.broker = PaperBroker(self.account, ExecutionConfig(slippage_pct=0.1),
                                  ExitConfig())

    def test_entry_pays_slippage(self):
        order = {"symbol": "AAA", "shares": 1.0, "risk_per_share": 5.0,
                 "reward_per_share": 12.5}
        self.broker.execute_entries([order], {"AAA": 100.0}, MON)
        self.assertAlmostEqual(self.account.positions["AAA"].entry_price, 100.1)

    def test_entry_preserves_risk_distance_across_a_gap(self):
        """Stop and target come from yesterday's close. Preserving the DISTANCE
        rather than the price means an overnight gap does not silently change
        the risk taken."""
        order = {"symbol": "AAA", "shares": 1.0, "risk_per_share": 5.0,
                 "reward_per_share": 12.5}
        self.broker.execute_entries([order], {"AAA": 110.0}, MON)
        position = self.account.positions["AAA"]
        self.assertAlmostEqual(position.entry_price - position.stop, 5.0)
        self.assertAlmostEqual(position.target - position.entry_price, 12.5)

    def test_stop_exit_executes(self):
        self.account.buy("AAA", 2.0, 100.0, MON, stop=95.0, target=112.5)
        fills, signals = self.broker.manage_exits(
            {"AAA": {"close": 94.0, "open": 96.0, "high": 97.0, "low": 93.0}},
            date(2026, 8, 10))
        self.assertEqual(len(fills), 1)
        self.assertNotIn("AAA", self.account.positions)
        self.assertAlmostEqual(self.account.closed[0].exit_price, 95.0)

    def test_gap_through_the_stop_fills_at_the_open(self):
        self.account.buy("AAA", 2.0, 100.0, MON, stop=95.0, target=112.5)
        self.broker.manage_exits(
            {"AAA": {"close": 89.0, "open": 90.0, "high": 91.0, "low": 88.0}},
            date(2026, 8, 10))
        self.assertAlmostEqual(self.account.closed[0].exit_price, 90.0)

    def test_target_trims_half_and_raises_the_stop(self):
        self.account.buy("AAA", 2.0, 100.0, MON, stop=95.0, target=112.5)
        self.broker.manage_exits(
            {"AAA": {"close": 113.0, "open": 111.0, "high": 114.0, "low": 110.0}},
            date(2026, 8, 10))
        self.assertAlmostEqual(self.account.positions["AAA"].shares, 1.0)
        self.assertGreaterEqual(self.account.positions["AAA"].stop, 100.0)

    def test_hold_ratchets_the_stop_without_trading(self):
        self.account.buy("AAA", 2.0, 100.0, MON, stop=95.0, target=125.0)
        fills, _ = self.broker.manage_exits(
            {"AAA": {"close": 106.0, "open": 105.0, "high": 107.0, "low": 104.0,
                     "ma50": 99.0}}, date(2026, 8, 10))
        self.assertEqual(fills, [])
        self.assertAlmostEqual(self.account.positions["AAA"].stop, 100.0)


if __name__ == "__main__":
    unittest.main(verbosity=1)
