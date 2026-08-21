"""Plain-text rollup for the CLI."""

from __future__ import annotations

import time
from typing import Any

BAR = "─" * 58


def _money(x: float) -> str:
    if abs(x) >= 1:
        return f"${x:,.2f}"
    if abs(x) >= 0.01:
        return f"${x:.4f}"
    return f"${x:.6f}"


def render_text(s: dict[str, Any]) -> str:
    if not s.get("requests"):
        return "No requests recorded yet."

    w = s["window"]
    lines = [
        BAR,
        "  tokenwise — cost report",
        f"  {time.strftime('%Y-%m-%d %H:%M', time.localtime(w['start']))}"
        f"  →  {time.strftime('%Y-%m-%d %H:%M', time.localtime(w['end']))}",
        BAR,
        f"  requests            {s['requests']:,}",
        f"  baseline cost       {_money(s['baseline_cost'])}",
        f"  actual cost         {_money(s['actual_cost'])}",
        f"  saved               {_money(s['saved'])}  ({s['saved_pct']:.1f}%)",
        "",
        "  savings by strategy",
    ]

    strategies = sorted(s["by_strategy"].items(), key=lambda kv: -kv[1])
    peak = max((abs(v) for _, v in strategies), default=1.0) or 1.0
    for name, val in strategies:
        width = int(abs(val) / peak * 28)
        glyph = ("█" if val >= 0 else "▒") * max(width, 1 if val else 0)
        lines.append(f"    {name:<16} {_money(val):>12}  {glyph}")

    lines += ["", "  spend by model"]
    for model, m in sorted(s["by_model"].items(), key=lambda kv: -kv[1]["cost"]):
        lines.append(
            f"    {model:<24} {m['requests']:>5} req  {_money(m['cost']):>12}"
            f"   in {int(m['input']):>9,}  out {int(m['output']):>8,}"
        )

    t = s["tokens"]
    lines += [
        "",
        f"  tokens              in {t['input']:,}   out {t['output']:,}"
        f"   cache-write {t['cache_write']:,}   cache-read {t['cache_read']:,}",
        f"  semantic hit rate   {s['cache_hit_rate'] * 100:.1f}%",
        f"  escalation rate     {s['escalation_rate'] * 100:.1f}%",
        f"  avg latency         {s['avg_latency_ms']:.0f} ms",
        BAR,
    ]
    return "\n".join(lines)
