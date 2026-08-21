"""Backtesting. Read docs/backtest.md before trusting a number out of here."""
from .engine import BacktestConfig, BacktestResult, run_backtest
from .metrics import performance
from .panels import Panels, build_panels
from .walkforward import walk_forward

__all__ = ["build_panels", "Panels", "run_backtest", "BacktestConfig",
           "BacktestResult", "performance", "walk_forward"]
