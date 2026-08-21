"""tokenwise command line."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import webbrowser

from . import dashboard, report
from .agent import TokenWiseAgent, text_of
from .config import Config
from .ledger import Ledger
from .pricing import PRICES


def _since(arg: str | None) -> float | None:
    if not arg:
        return None
    units = {"h": 3600, "d": 86400, "w": 604800}
    if arg[-1] in units and arg[:-1].isdigit():
        return time.time() - int(arg[:-1]) * units[arg[-1]]
    raise SystemExit(f"bad --since value: {arg!r} (try 24h, 7d, 2w)")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="tokenwise", description=__doc__)
    p.add_argument("--ledger", default=os.environ.get("TOKENWISE_LEDGER", ".tokenwise/ledger.jsonl"))
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("ask", help="send a prompt through the optimizer")
    a.add_argument("prompt", nargs="+")
    a.add_argument("--system")
    a.add_argument("--model", default="claude-opus-5", help="baseline model (may be routed down)")
    a.add_argument("--max-tokens", type=int, default=1024)
    a.add_argument("--routing", default="heuristic", choices=["heuristic", "classifier", "off"])
    a.add_argument("--no-semantic-cache", action="store_true")
    a.add_argument("-q", "--quiet", action="store_true")

    s = sub.add_parser("stats", help="print the cost report")
    s.add_argument("--since", help="24h, 7d, 2w")
    s.add_argument("--json", action="store_true")

    d = sub.add_parser("dashboard", help="write the HTML dashboard")
    d.add_argument("-o", "--out", default="tokenwise-dashboard.html")
    d.add_argument("--since")
    d.add_argument("--baseline", default="claude-opus-5")
    d.add_argument("--open", action="store_true")

    sub.add_parser("pricing", help="show the pricing table in use")

    sim = sub.add_parser("simulate", help="run a mock workload — no API key needed")
    sim.add_argument("-n", type=int, default=200, help="number of requests")
    sim.add_argument("-o", "--out", default="tokenwise-dashboard.html")

    args = p.parse_args(argv)

    if args.cmd == "ask":
        agent = TokenWiseAgent(
            Config(
                baseline_model=args.model,
                routing=args.routing,
                semantic_cache=not args.no_semantic_cache,
                ledger_path=args.ledger,
                max_tokens=args.max_tokens,
            )
        )
        resp = agent.create(
            model=args.model,
            system=args.system,
            max_tokens=args.max_tokens,
            messages=[{"role": "user", "content": " ".join(args.prompt)}],
        )
        print(text_of(resp))
        r = agent.last_record
        if r and not args.quiet:
            print(
                f"\n\033[2m[{r.model} · ${r.actual_cost:.5f} · saved ${r.total_saved:.5f} "
                f"vs {r.baseline_model}"
                + (f" · {', '.join(r.notes)}" if r.notes else "")
                + "]\033[0m",
                file=sys.stderr,
            )
        agent.close()
        return 0

    if args.cmd == "stats":
        summary = Ledger(args.ledger).summary(_since(args.since))
        print(json.dumps(summary, indent=2, default=str) if args.json else report.render_text(summary))
        return 0

    if args.cmd == "dashboard":
        summary = Ledger(args.ledger).summary(_since(args.since))
        if not summary.get("requests"):
            print("No requests in the ledger yet. Try `tokenwise simulate`.")
            return 1
        out = dashboard.write(summary, args.out, baseline_model=args.baseline)
        print(f"wrote {out}")
        if args.open:
            webbrowser.open(f"file://{os.path.abspath(out)}")
        return 0

    if args.cmd == "pricing":
        print(f"{'model':<24}{'input':>10}{'output':>10}{'cache write':>14}{'cache read':>12}")
        for model, pr in PRICES.items():
            print(
                f"{model:<24}{pr.input:>10.2f}{pr.output:>10.2f}"
                f"{pr.cache_write_5m:>14.2f}{pr.cache_read:>12.2f}"
            )
        print("\nUSD per million tokens. Override with TOKENWISE_PRICING=path/to/prices.json")
        return 0

    if args.cmd == "simulate":
        from .simulate import run

        run(n=args.n, ledger_path=args.ledger, out=args.out)
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
