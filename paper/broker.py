"""Paper execution: turn decisions into fills at realistic prices.

Same timing model as the backtest, and for the same reason -- if paper trading
used a friendlier fill model than the backtest, the two would disagree and you
would not know which to believe.

    decisions from the CLOSE of day D  ->  fills at the OPEN of day D+1

Exits are the exception: a stop is checked against the day's LOW and fills at
the stop price, or at the open when the stock gapped through it. A position
that traded through its stop is out, whatever it closed at.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

from core.exits import SELL, TRIM, ExitConfig, ExitSignal, evaluate_book
from paper.account import Account

log = logging.getLogger(__name__)


@dataclass
class ExecutionConfig:
    slippage_pct: float = 0.05        # per side, same default as the backtest
    commission: float = 0.0           # most retail brokers are zero now


class PaperBroker:
    def __init__(self, account: Account, config: ExecutionConfig | None = None,
                 exits: ExitConfig | None = None):
        self.account = account
        self.cfg = config or ExecutionConfig()
        self.exits = exits or ExitConfig.from_yaml()

    # -- entries ------------------------------------------------------------

    def execute_entries(self, orders: list[dict], opens: dict[str, float],
                        on: date) -> list:
        """Fill pending orders at today's open, best-ranked first.

        Order matters when cash is tight: the account should spend what it has
        on the highest-conviction names, not on whichever happened to be first
        alphabetically.
        """
        fills = []
        for order in orders:
            price = opens.get(order["symbol"])
            if not price or price <= 0:
                continue
            fill_price = price * (1 + self.cfg.slippage_pct / 100.0)
            costs = self.cfg.commission

            # Stop and target were computed from yesterday's close. Preserve
            # the DISTANCE, not the price, so an overnight gap does not silently
            # change the risk taken.
            stop = fill_price - order["risk_per_share"]
            target = fill_price + order["reward_per_share"]

            fill = self.account.buy(
                order["symbol"], order["shares"], fill_price, on,
                stop=stop, target=target, costs=costs,
                sector=order.get("sector", "UNKNOWN"),
                thesis=order.get("thesis", ""),
                invalidation=order.get("invalidation", ""))
            if fill:
                fills.append(fill)
        return fills

    # -- exits --------------------------------------------------------------

    def manage_exits(self, quotes: dict[str, dict], on: date) -> tuple[list, list[ExitSignal]]:
        """Apply the exit rules and execute anything that fires."""
        positions = list(self.account.positions.values())
        signals = evaluate_book(positions, quotes, as_of=on, config=self.exits)

        fills = []
        for signal in signals:
            position = self.account.positions.get(signal.symbol)
            if position is None:
                continue
            quote = quotes.get(signal.symbol, {})

            if signal.action == SELL:
                price = self._exit_price(signal, position, quote)
                fill = self.account.sell(signal.symbol, price, on,
                                         costs_pct=self.cfg.slippage_pct,
                                         reason=signal.reason)
                if fill:
                    fills.append(fill)
            elif signal.action == TRIM:
                price = float(quote.get("close", position.target))
                fill = self.account.sell(signal.symbol, price, on,
                                         fraction=signal.fraction,
                                         costs_pct=self.cfg.slippage_pct,
                                         reason=signal.reason + "_partial")
                if fill:
                    fills.append(fill)
                remaining = self.account.positions.get(signal.symbol)
                if remaining and signal.suggested_stop:
                    remaining.stop = max(remaining.stop, signal.suggested_stop)
            elif signal.suggested_stop:
                # HOLD or WATCH: ratchet the stop, do not trade.
                position.stop = max(position.stop, signal.suggested_stop)

        return fills, signals

    def _exit_price(self, signal: ExitSignal, position, quote: dict) -> float:
        if signal.reason == "stop":
            open_px = quote.get("open")
            # Gapped below the stop -> you get the open, not the stop. Modelling
            # every stop as filling exactly at the stop is how a simulation
            # understates its own drawdown.
            if open_px and float(open_px) < position.stop:
                return float(open_px)
            return position.stop
        if signal.reason == "target":
            open_px = quote.get("open")
            if open_px and float(open_px) > position.target:
                return float(open_px)
            return position.target
        return float(quote.get("close", position.entry_price))
