"""Cost attribution report from the usage ledger (no UI on purpose).

    python -m sluice.report --by team
    python -m sluice.report --by app --since 2026-10-01
    python -m sluice.report --trace <trace id from the x-sluice-trace-id header>
    python -m sluice.report --mcp           # MCP tool calls by key, tool and outcome
    python -m sluice.report --security      # suspended keys, refused calls, secrets, pins
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
    ap.add_argument("--mcp", action="store_true", help="MCP tool-call audit summary")
    ap.add_argument("--security", action="store_true",
                    help="what containment did: suspensions, refused tool calls, secrets, pins")
    a = ap.parse_args(argv)
    if a.security:
        print(security(a.db, a.since))
        return
    if a.mcp:
        from .mcp.audit import McpAudit

        rows = McpAudit(a.db).summary()
        w = max([4] + [len(str(r[1])) for r in rows])
        print(f"{'key':<16} {'tool':<{w}} {'outcome':<14} calls")
        for k, tool, outcome, n in rows:
            print(f"{k or '-':<16} {tool:<{w}} {outcome:<14} {n}")
        return
    ledger = UsageLedger(a.db)
    if a.trace:
        row = ledger.by_trace(a.trace)
        print("\n".join(f"{k:>18}: {v}" for k, v in row.items()) if row else "no such trace")
        return
    print(render(ledger.summary(a.by, a.since), a.by))


def security(db: str, since: str | None = None) -> str:
    """For the incident runbook (docs/runbook-key-compromise.md, step 4): what the gateway's own
    records say. The watch's alerts live in memory and logs; this reads what's on disk."""
    import json
    from collections import Counter

    from .auth.keys import KeyStore
    from .mcp.audit import McpAudit
    from .mcp.pins import ToolPins

    keys, audit, pins = KeyStore(db), McpAudit(db), ToolPins(db)
    out = ["Keys not active:"]
    for k, hint in keys.list():
        if k.status != "active":
            out.append(f"  #{k.id:<4} {k.name:<20} team={k.team or '-':<10} ...{hint}  "
                       f"{k.status}{': ' + k.suspended_reason if k.suspended_reason else ''}")
    if keys.flag("paid_off"):
        out.append("  LOCKDOWN: paid providers are off")
    rows = audit.outcomes(since)
    refused = Counter((r[0], r[2]) for r in rows if r[2] in (
        "denied", "unknown", "pending", "quarantined", "egress_blocked", "rate_limited"))
    out.append(f"Refused tool calls{' since ' + since if since else ''}:")
    out += [f"  {k or '-':<20} {o:<15} {n}" for (k, o), n in sorted(refused.items())] or ["  none"]
    secrets = Counter()
    for k, tool, _, egress in rows:
        for e in json.loads(egress or "[]"):
            secrets[(k, tool, e["where"], e["type"])] += 1
    out.append("Secrets seen in tool traffic (type and place only):")
    out += [f"  {k or '-':<20} {t:<28} {w:<13} {ty} x{n}"
            for (k, t, w, ty), n in sorted(secrets.items())] or ["  none"]
    held = [r for r in pins.rows() if r["status"] != "approved"]
    out.append("Tools held (pending or quarantined):")
    out += [f"  {r['tool']:<32} {r['status']:<12} changed={r['changed_at'] or '-'}"
            for r in held] or ["  none"]
    return "\n".join(out)


if __name__ == "__main__":
    main()
