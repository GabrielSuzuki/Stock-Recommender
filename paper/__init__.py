"""Paper trading — the real system, virtual money.

Run the actual screen, the actual sizing, the actual exit rules, against a
simulated account. The point is to find out whether the process is worth
funding before it is funded.
"""
from .account import Account, Deposit, PaperFill
from .broker import PaperBroker

__all__ = ["Account", "Deposit", "PaperFill", "PaperBroker"]
