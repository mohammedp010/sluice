"""Cost attribution report from the usage ledger (no UI on purpose).

    python -m sluice.report --by team
    python -m sluice.report --by app --since 2026-10-01
    python -m sluice.report --trace <trace id from the x-sluice-trace-id header>
"""
from __future__ import annotations

import argparse

from .telemetry.ledger import GROUPS, UsageLedger

HEAD = ("requests", "errors", "fell over", "tokens in", "tokens out", "cost USD")


def render(rows: list[tuple], by: str) -> str:
    w = max([len(by)] + [len(str(r[0])) for r in rows])
    out = [f"{by:<{w}}  " + "  ".join(f"{h:>10}" for h in HEAD)]
    for g, *nums in rows:
        *ints, cost = [n or 0 for n in nums]
        out.append(f"{g:<{w}}  " + "  ".join(f"{n:>10}" for n in ints) + f"  {cost:>10.6f}")
    if rows:
        tot = [sum((r[i] or 0) for r in rows) for i in range(1, 7)]
        out.append(f"{'TOTAL':<{w}}  " + "  ".join(f"{n:>10}" for n in tot[:5])
                   + f"  {tot[5]:>10.6f}")
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="sluice-report")
    ap.add_argument("--db", default="sluice.db")
    ap.add_argument("--by", choices=sorted(GROUPS), default="team")
    ap.add_argument("--since", help="UTC date or timestamp prefix, e.g. 2026-10-01")
    ap.add_argument("--trace", help="show the ledger row for one trace id")
    a = ap.parse_args(argv)
    ledger = UsageLedger(a.db)
    if a.trace:
        row = ledger.by_trace(a.trace)
        print("\n".join(f"{k:>18}: {v}" for k, v in row.items()) if row else "no such trace")
        return
    print(render(ledger.summary(a.by, a.since), a.by))


if __name__ == "__main__":
    main()
