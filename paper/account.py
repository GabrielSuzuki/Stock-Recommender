"""A paper account: cash, deposits that carry over, positions, equity history.

Design decisions made for the $100/week test, and why:

**Fractional shares are enabled.** With $400 of equity and 0.75% risk per
trade, the whole-share model produces zero shares on any stock above about $20
-- the account would sit in cash for a month and prove nothing. Every major
retail broker now supports fractional shares, so this is realistic rather than
a fudge. It does mean the *dollar* results at this size are tiny; the
percentage results are what transfers.

**Deposits carry over.** Unused cash accumulates. Week 4 can buy what week 1
could not, which is the whole reason for running four weeks rather than one.

**Cash is never negative.** An order that cannot be paid for is rejected and
recorded, not filled on margin. The rejection count is itself a finding: it
tells you the account was too small for the strategy's position sizes.

State is JSON on disk, so a forward-running test survives restarts.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from core.exits import Position


@dataclass
class Deposit:
    on: date
    amount: float

    def to_dict(self) -> dict:
        return {"on": str(self.on), "amount": self.amount}


@dataclass
class PaperFill:
    on: date
    symbol: str
    side: str                     # buy | sell
    shares: float
    price: float
    costs: float = 0.0
    reason: str = ""

    @property
    def gross(self) -> float:
        return self.shares * self.price

    def to_dict(self) -> dict:
        return {"on": str(self.on), "symbol": self.symbol, "side": self.side,
                "shares": round(self.shares, 6), "price": round(self.price, 4),
                "costs": round(self.costs, 4), "reason": self.reason,
                "gross": round(self.gross, 2)}


@dataclass
class ClosedTrade:
    symbol: str
    entry_date: date
    exit_date: date
    entry_price: float
    exit_price: float
    shares: float
    costs: float
    reason: str
    risk_per_share: float

    @property
    def pnl(self) -> float:
        return self.shares * (self.exit_price - self.entry_price) - self.costs

    @property
    def r_multiple(self) -> float:
        risk = self.shares * self.risk_per_share
        return self.pnl / risk if risk > 0 else 0.0

    @property
    def return_pct(self) -> float:
        return (self.exit_price / self.entry_price - 1.0) * 100.0

    def to_dict(self) -> dict:
        return {"symbol": self.symbol, "entry_date": str(self.entry_date),
                "exit_date": str(self.exit_date),
                "entry_price": round(self.entry_price, 4),
                "exit_price": round(self.exit_price, 4),
                "shares": round(self.shares, 6), "costs": round(self.costs, 2),
                # Must be persisted: R is anchored to the risk actually taken at
                # entry, and it cannot be recovered from entry/exit prices. An
                # earlier version omitted it and silently fell back to a 5%
                # estimate, which doubled every R-multiple after a restart.
                "risk_per_share": round(self.risk_per_share, 6),
                "reason": self.reason, "pnl": round(self.pnl, 2),
                "r_multiple": round(self.r_multiple, 3),
                "return_pct": round(self.return_pct, 2)}


@dataclass
class Account:
    cash: float = 0.0
    positions: dict[str, Position] = field(default_factory=dict)
    deposits: list[Deposit] = field(default_factory=list)
    fills: list[PaperFill] = field(default_factory=list)
    closed: list[ClosedTrade] = field(default_factory=list)
    equity_history: list[dict] = field(default_factory=list)
    rejected: list[dict] = field(default_factory=list)
    allow_fractional: bool = True

    # -- money --------------------------------------------------------------

    @property
    def deposited(self) -> float:
        return sum(d.amount for d in self.deposits)

    def deposit(self, amount: float, on: date) -> None:
        if amount <= 0:
            raise ValueError("deposit must be positive")
        self.cash += amount
        self.deposits.append(Deposit(on=on, amount=amount))

    def market_value(self, prices: dict[str, float]) -> float:
        total = 0.0
        for symbol, position in self.positions.items():
            price = prices.get(symbol)
            # A position with no quote is held at cost rather than dropped to
            # zero. Marking an unquoted holding to zero would show a fake
            # drawdown; marking it at cost is the honest "we do not know yet".
            total += position.shares * (price if price else position.entry_price)
        return total

    def equity(self, prices: dict[str, float]) -> float:
        return self.cash + self.market_value(prices)

    def profit(self, prices: dict[str, float]) -> float:
        """Gain over money deposited -- the only meaningful return measure when
        capital is being added over time. A raw equity curve rises simply
        because you kept paying in."""
        return self.equity(prices) - self.deposited

    def return_pct(self, prices: dict[str, float]) -> float:
        return (self.profit(prices) / self.deposited * 100.0) if self.deposited else 0.0

    # -- orders -------------------------------------------------------------

    def can_afford(self, shares: float, price: float, costs: float = 0.0) -> bool:
        return shares * price + costs <= self.cash + 1e-9

    def buy(self, symbol: str, shares: float, price: float, on: date,
            *, stop: float, target: float, costs: float = 0.0,
            sector: str = "UNKNOWN", thesis: str = "",
            invalidation: str = "") -> PaperFill | None:
        if not self.allow_fractional:
            shares = float(int(shares))
        if shares <= 0:
            return None
        if not self.can_afford(shares, price, costs):
            self.rejected.append({"on": str(on), "symbol": symbol, "reason": "cash",
                                  "needed": round(shares * price + costs, 2),
                                  "available": round(self.cash, 2)})
            return None

        self.cash -= shares * price + costs
        if symbol in self.positions:
            # Averaging up muddies R and the exit rules. One position per name.
            existing = self.positions[symbol]
            total = existing.shares + shares
            existing.entry_price = ((existing.entry_price * existing.shares
                                     + price * shares) / total)
            existing.shares = total
        else:
            self.positions[symbol] = Position(
                symbol=symbol, entry_date=on, entry_price=price, shares=shares,
                stop=stop, target=target, initial_stop=stop, sector=sector,
                thesis=thesis, invalidation=invalidation)

        fill = PaperFill(on=on, symbol=symbol, side="buy", shares=shares,
                         price=price, costs=costs, reason="entry")
        self.fills.append(fill)
        return fill

    def sell(self, symbol: str, price: float, on: date, *, fraction: float = 1.0,
             costs_pct: float = 0.0, reason: str = "") -> PaperFill | None:
        position = self.positions.get(symbol)
        if position is None:
            return None
        shares = position.shares * min(max(fraction, 0.0), 1.0)
        if shares <= 0:
            return None

        proceeds = shares * price
        costs = proceeds * costs_pct / 100.0
        self.cash += proceeds - costs

        self.closed.append(ClosedTrade(
            symbol=symbol, entry_date=position.entry_date, exit_date=on,
            entry_price=position.entry_price, exit_price=price, shares=shares,
            costs=costs, reason=reason, risk_per_share=position.risk_per_share))

        position.shares -= shares
        if position.shares <= 1e-9:
            del self.positions[symbol]
        else:
            position.scaled_out = True

        fill = PaperFill(on=on, symbol=symbol, side="sell", shares=shares,
                         price=price, costs=costs, reason=reason)
        self.fills.append(fill)
        return fill

    # -- bookkeeping --------------------------------------------------------

    def mark(self, on: date, prices: dict[str, float]) -> dict:
        snapshot = {"on": str(on), "cash": round(self.cash, 2),
                    "positions": len(self.positions),
                    "market_value": round(self.market_value(prices), 2),
                    "equity": round(self.equity(prices), 2),
                    "deposited": round(self.deposited, 2),
                    "profit": round(self.profit(prices), 2)}
        self.equity_history.append(snapshot)
        return snapshot

    # -- persistence --------------------------------------------------------

    def to_dict(self) -> dict:
        return {"cash": round(self.cash, 6),
                "allow_fractional": self.allow_fractional,
                "positions": [p.to_dict() for p in self.positions.values()],
                "deposits": [d.to_dict() for d in self.deposits],
                "fills": [f.to_dict() for f in self.fills],
                "closed": [c.to_dict() for c in self.closed],
                "equity_history": self.equity_history,
                "rejected": self.rejected}

    def save(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.to_dict(), indent=2, default=str))
        tmp.replace(path)          # atomic: a crash cannot truncate the account

    @classmethod
    def load(cls, path: str | Path) -> "Account":
        path = Path(path)
        if not path.exists():
            return cls()
        raw = json.loads(path.read_text())
        account = cls(cash=float(raw.get("cash", 0.0)),
                      allow_fractional=bool(raw.get("allow_fractional", True)))
        account.positions = {p["symbol"]: Position.from_dict(p)
                             for p in raw.get("positions", [])}
        account.deposits = [Deposit(on=date.fromisoformat(d["on"]),
                                    amount=float(d["amount"]))
                            for d in raw.get("deposits", [])]
        account.fills = [PaperFill(on=date.fromisoformat(f["on"]), symbol=f["symbol"],
                                   side=f["side"], shares=float(f["shares"]),
                                   price=float(f["price"]), costs=float(f.get("costs", 0)),
                                   reason=f.get("reason", ""))
                         for f in raw.get("fills", [])]
        account.closed = [ClosedTrade(
            symbol=c["symbol"], entry_date=date.fromisoformat(c["entry_date"]),
            exit_date=date.fromisoformat(c["exit_date"]),
            entry_price=float(c["entry_price"]), exit_price=float(c["exit_price"]),
            shares=float(c["shares"]), costs=float(c.get("costs", 0)),
            reason=c.get("reason", ""),
            risk_per_share=float(c["risk_per_share"]))
            for c in raw.get("closed", [])]
        account.equity_history = raw.get("equity_history", [])
        account.rejected = raw.get("rejected", [])
        return account
