#!/usr/bin/env python3
"""Run the screen on a synthetic universe and print what it saw.

    python3 core/demo.py

No API keys, no network. This is the shape of Stage A's console output --
the funnel is the part to read on a day when nothing passes.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from core import synthetic as syn
from core.ranking import top_candidates
from core.screen import ScreenConfig, run_screen

pd.set_option("display.width", 120)
pd.set_option("display.max_columns", 30)


def main() -> int:
    bars = syn.universe()
    cfg = ScreenConfig.from_yaml()
    result = run_screen(bars, config=cfg)

    s = result.summary()
    print(f"\n{'='*72}\nSTAGE A — screen run\n{'='*72}")
    print(f"universe                {s['universe_size']}")
    print(f"insufficient history    {s['insufficient_history']}  {result.skipped_symbols}")
    print(f"evaluated               {s['evaluated']}")
    print(f"passed gates            {s['passed_gates']}")
    print(f"passed template         {s['passed_template']}")

    print(f"\n{'-'*72}\nFUNNEL — what killed what\n{'-'*72}")
    print(result.funnel().to_string(index=False))

    print(f"\n{'-'*72}\nFIRST FAILED CONDITION, per rejected name\n{'-'*72}")
    reasons = result.rejection_reasons()
    if reasons.empty:
        print("(nothing rejected)")
    else:
        for symbol, reason in reasons.sort_values().items():
            print(f"  {symbol:<10} {reason}")

    top = top_candidates(result)
    print(f"\n{'-'*72}\nCANDIDATES — {len(top)} name(s), best first\n{'-'*72}")
    if top.empty:
        print("  No setups today. This is a valid answer, not a failure.")
    else:
        cols = ["close", "rs_rank", "pct_below_52wk_high", "ma200_slope_pct",
                "volume_trend", "atr_pct", "rank_score"]
        print(top[cols].round(2).to_string())

    if top.attrs.get("truncated"):
        print(f"\n  note: {top.attrs['truncated']} further name(s) passed but were "
              f"cut at max_candidates={cfg.max_candidates}")

    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
