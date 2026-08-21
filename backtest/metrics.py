"""Performance metrics, including the ones that make a strategy look bad.

A backtest report that omits max drawdown, exposure, and the benchmark null is
marketing, not evidence. Everything here is reported whether or not it flatters
the strategy.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

TRADING_DAYS = 252


def cagr(equity: pd.Series) -> float:
    if len(equity) < 2 or equity.iloc[0] <= 0:
        return float("nan")
    years = len(equity) / TRADING_DAYS
    if years <= 0:
        return float("nan")
    growth = equity.iloc[-1] / equity.iloc[0]
    if growth <= 0:
        return -1.0
    return growth ** (1 / years) - 1.0


def max_drawdown(equity: pd.Series) -> float:
    """Worst peak-to-trough decline, as a negative fraction."""
    if equity.empty:
        return float("nan")
    peak = equity.cummax()
    return float((equity / peak - 1.0).min())


def drawdown_duration_days(equity: pd.Series) -> int:
    """Longest stretch spent below a prior peak.

    Often more decisive than the depth: a 20% drawdown that recovers in six
    weeks is survivable, and the same 20% spread over two years is what makes
    people abandon a system that was working.
    """
    if equity.empty:
        return 0
    peak = equity.cummax()
    underwater = equity < peak
    longest = current = 0
    for flag in underwater:
        current = current + 1 if flag else 0
        longest = max(longest, current)
    return longest


def sharpe(equity: pd.Series, risk_free: float = 0.0) -> float:
    returns = equity.pct_change().dropna()
    if len(returns) < 2 or returns.std(ddof=1) == 0:
        return float("nan")
    excess = returns - risk_free / TRADING_DAYS
    return float(excess.mean() / returns.std(ddof=1) * math.sqrt(TRADING_DAYS))


def sortino(equity: pd.Series) -> float:
    """Sharpe's more honest cousin: only downside deviation is penalised."""
    returns = equity.pct_change().dropna()
    downside = returns[returns < 0]
    if len(returns) < 2 or downside.empty or downside.std(ddof=1) == 0:
        return float("nan")
    return float(returns.mean() / downside.std(ddof=1) * math.sqrt(TRADING_DAYS))


def trade_stats(trades: list) -> dict:
    closed = [t for t in trades if not t.open]
    if not closed:
        # Every key the report and formatter expect. Returning a partial dict
        # here means "no trades" crashes the report rather than reporting zero
        # trades -- and a strategy that takes no trades is a result worth
        # printing, not an error.
        return {"trades": 0, "win_rate": 0.0, "avg_win": 0.0, "avg_loss": 0.0,
                "avg_r": 0.0, "expectancy_r": 0.0, "profit_factor": 0.0,
                "best_r": 0.0, "worst_r": 0.0, "avg_hold_days": 0.0,
                "total_costs": 0.0, "exit_reasons": {}}

    rs = np.array([t.r_multiple for t in closed])
    pnls = np.array([t.pnl for t in closed])
    wins, losses = pnls[pnls > 0], pnls[pnls <= 0]

    reasons: dict[str, int] = {}
    for t in closed:
        reasons[t.exit_reason] = reasons.get(t.exit_reason, 0) + 1

    gross_loss = abs(losses.sum())
    return {
        "trades": len(closed),
        "win_rate": round(float(len(wins) / len(closed) * 100), 1),
        "avg_win": round(float(wins.mean()), 2) if len(wins) else 0.0,
        "avg_loss": round(float(losses.mean()), 2) if len(losses) else 0.0,
        "avg_r": round(float(rs.mean()), 3),
        # Expectancy in R is the number that actually matters: how much you
        # make per unit risked, on average, including the losers.
        "expectancy_r": round(float(rs.mean()), 3),
        "profit_factor": round(float(wins.sum() / gross_loss), 2)
        if gross_loss > 0 else float("inf"),
        "best_r": round(float(rs.max()), 2),
        "worst_r": round(float(rs.min()), 2),
        "avg_hold_days": round(float(np.mean([t.hold_days for t in closed])), 1),
        "total_costs": round(float(sum(t.costs for t in closed)), 2),
        "exit_reasons": reasons,
    }


def exposure(result) -> float:
    """Percent of days with at least one position open.

    A strategy that is in the market 8% of the time and returns 6% a year is a
    very different proposition from one that is fully invested for the same
    return -- and the raw return alone cannot tell them apart.
    """
    if not result.trades or result.equity.empty:
        return 0.0
    days = pd.Series(0, index=result.equity.index)
    for trade in result.trades:
        end = trade.exit_date or result.equity.index[-1]
        days.loc[trade.entry_date:end] = 1
    return round(float(days.mean() * 100), 1)


def performance(result) -> dict:
    """The full report. Always includes the benchmark null."""
    equity = result.equity
    report = {
        "start": str(equity.index[0].date()) if not equity.empty else None,
        "end": str(equity.index[-1].date()) if not equity.empty else None,
        "trading_days": len(equity),
        "starting_equity": round(float(equity.iloc[0]), 2) if not equity.empty else 0.0,
        "ending_equity": round(float(equity.iloc[-1]), 2) if not equity.empty else 0.0,
        "total_return_pct": round(float(equity.iloc[-1] / equity.iloc[0] - 1) * 100, 2)
        if len(equity) > 1 else 0.0,
        "cagr_pct": round(cagr(equity) * 100, 2),
        "max_drawdown_pct": round(max_drawdown(equity) * 100, 2),
        "longest_drawdown_days": drawdown_duration_days(equity),
        "sharpe": round(sharpe(equity), 2),
        "sortino": round(sortino(equity), 2),
        "exposure_pct": exposure(result),
        "regime_days": result.regime_days,
        "skipped_no_capacity": result.skipped_no_capacity,
        **trade_stats(result.trades),
    }

    # The null. Reported every time, per architecture.md §10 rule 5: if the
    # strategy does not clear buy-and-hold after costs and your time, that is a
    # real and useful finding, and it is only visible if you always measure it.
    bench = result.benchmark_equity
    if bench is not None and len(bench) > 1:
        report["benchmark"] = {
            "symbol": result.config.benchmark,
            "total_return_pct": round(float(bench.iloc[-1] / bench.iloc[0] - 1) * 100, 2),
            "cagr_pct": round(cagr(bench) * 100, 2),
            "max_drawdown_pct": round(max_drawdown(bench) * 100, 2),
            "sharpe": round(sharpe(bench), 2),
        }
        report["excess_return_pct"] = round(
            report["total_return_pct"] - report["benchmark"]["total_return_pct"], 2)
        report["beat_benchmark"] = report["excess_return_pct"] > 0
    else:
        report["benchmark"] = None
        report["excess_return_pct"] = None
        report["beat_benchmark"] = None

    return report


def format_report(report: dict, title: str = "BACKTEST") -> str:
    """Plain-text summary for the console and the weekly review."""
    lines = [f"{'=' * 64}", title, "=" * 64,
             f"  period            {report['start']} -> {report['end']} "
             f"({report['trading_days']} days)",
             f"  equity            ${report['starting_equity']:,.0f} -> "
             f"${report['ending_equity']:,.0f}",
             f"  total return      {report['total_return_pct']:+.2f}%",
             f"  CAGR              {report['cagr_pct']:+.2f}%",
             f"  max drawdown      {report['max_drawdown_pct']:.2f}%  "
             f"(longest {report['longest_drawdown_days']}d underwater)",
             f"  Sharpe / Sortino  {report['sharpe']} / {report['sortino']}",
             f"  exposure          {report['exposure_pct']}% of days",
             "",
             f"  trades            {report['trades']}",
             f"  win rate          {report['win_rate']}%",
             f"  expectancy        {report['expectancy_r']:+.3f}R",
             f"  profit factor     {report['profit_factor']}",
             f"  avg hold          {report['avg_hold_days']} days",
             f"  costs paid        ${report['total_costs']:,.2f}",
             f"  exits             {report['exit_reasons']}",
             f"  regime days       {report['regime_days']}"]

    if report.get("benchmark"):
        b = report["benchmark"]
        lines += ["", f"  {'-' * 60}",
                  f"  NULL: buy and hold {b['symbol']}",
                  f"    total return    {b['total_return_pct']:+.2f}%",
                  f"    CAGR            {b['cagr_pct']:+.2f}%",
                  f"    max drawdown    {b['max_drawdown_pct']:.2f}%",
                  f"    Sharpe          {b['sharpe']}",
                  "",
                  f"  EXCESS            {report['excess_return_pct']:+.2f}%  "
                  f"({'BEAT' if report['beat_benchmark'] else 'DID NOT BEAT'} the null)"]
    return "\n".join(lines)
