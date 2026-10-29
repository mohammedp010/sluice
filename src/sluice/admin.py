"""Tiny admin CLI:  python -m sluice.admin create-key --name demo --rpm 30

The database and the default key lifetime come from the policy in --policy or $SLUICE_POLICY
(auth.db_path, auth.key_ttl); --db overrides the path. With neither, ./sluice.db.
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
from datetime import UTC, datetime

from .auth.keys import TS, KeyStore, parse_duration


def _when(s: str) -> str:
    """'2026-10-29' or '2026-10-29T12:00' (UTC) -> the store's timestamp format."""
    t = datetime.fromisoformat(s)
    t = t.replace(tzinfo=UTC) if t.tzinfo is None else t.astimezone(UTC)
    return t.strftime(TS)


def _state(k) -> str:
    if k.status == "suspended":
        return f"  SUSPENDED ({k.suspended_reason})"
    return "  REVOKED" if k.revoked else ""


def main(argv=None):
    ap = argparse.ArgumentParser(prog="sluice-admin")
    ap.add_argument("--policy", default=os.environ.get("SLUICE_POLICY"),
                    help="policy file to read auth.db_path and auth.key_ttl from")
    ap.add_argument("--db", help="key database (default: the policy's auth.db_path)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create-key")
    c.add_argument("--name", required=True)
    c.add_argument("--team", default="")
    c.add_argument("--app", default="")
    c.add_argument("--models", default="*", help="comma-separated public model names, or *")
    c.add_argument("--rpm", type=int, default=60)
    c.add_argument("--tpm", type=int, default=20000)
    c.add_argument("--daily-budget", type=float)
    c.add_argument("--monthly-budget", type=float)
    c.add_argument("--tools", default="",
                   help='MCP tools as server__tool globs, comma-separated (e.g. "files__read_*")')
    c.add_argument("--ttl", help="lifetime, e.g. 30d or 12h (default: the policy's auth.key_ttl)")
    c.add_argument("--no-expiry", action="store_true", help="never expire, whatever the policy")
    c.add_argument("--tool-rpm", type=int, default=60, help="MCP tool calls per minute")
    c.add_argument("--cidrs", default="",
                   help="networks the key may be used from, comma-separated (e.g. 10.0.0.0/8)")
    sub.add_parser("list-keys")
    r = sub.add_parser("revoke-key")
    r.add_argument("id", type=int)
    rm = sub.add_parser("revoke", help="revoke many keys at once (all filters must match)")
    rm.add_argument("--team")
    rm.add_argument("--created-before", type=_when, help="UTC date or time, e.g. 2026-10-21")
    rm.add_argument("--all", action="store_true", help="every key")
    ro = sub.add_parser("rotate-key", help="new key, same grants; the old one works for --grace")
    ro.add_argument("id", type=int)
    ro.add_argument("--grace", default="24h", help="how long the old key keeps working")
    ro.add_argument("--ttl", help="the new key's lifetime (default: the old key's)")
    s = sub.add_parser("suspend-key", help="reversible: unsuspend-key restores it")
    s.add_argument("id", type=int)
    s.add_argument("--reason", default="manual")
    u = sub.add_parser("unsuspend-key")
    u.add_argument("id", type=int)
    pa = sub.add_parser("prune-audit", help="delete MCP audit rows older than --days "
                        "(checkpointed, so the hash chain still verifies)")
    pa.add_argument("--days", type=float, required=True)
    lk = sub.add_parser("lockdown", help="incident step 1: suspend every key in scope; without "
                        "--team, also turn paid providers off on every replica")
    lk.add_argument("--team")
    lk.add_argument("--lift", action="store_true",
                    help="undo: unsuspend what lockdown suspended (other suspensions stay)")
    t = sub.add_parser("tools", help="MCP tool pins: list, diff and approve (ADR 0011)")
    tsub = t.add_subparsers(dest="tcmd", required=True)
    tsub.add_parser("list")
    td = tsub.add_parser("diff", help="the trusted definition against the one seen last")
    td.add_argument("tool", help="public name, server__tool")
    ta = tsub.add_parser("approve", help="trust the definition seen last")
    ta.add_argument("tool")
    ta.add_argument("--sha", help="only if the definition seen last still has this sha256 prefix")
    a = ap.parse_args(argv)

    auth = settings = None
    if a.policy:
        from .config import load_settings
        settings = load_settings(a.policy)
        auth = settings.auth
    db = a.db or (auth.db_path if auth else "sluice.db")
    audit_db = a.db or (settings and settings.mcp.audit_db) or db
    if a.cmd == "tools":
        from .mcp.pins import ToolPins
        tools(ToolPins(audit_db), a)
        return
    if a.cmd == "prune-audit":
        from .mcp.audit import McpAudit
        print(f"deleted {McpAudit(audit_db).prune(a.days)} audit row(s)")
        return
    store = KeyStore(db)
    default_ttl = parse_duration(auth.key_ttl) if auth and auth.key_ttl else None

    if a.cmd == "create-key":
        ttl = None if a.no_expiry else parse_duration(a.ttl) if a.ttl else default_ttl
        raw, k = store.create(
            a.name, a.team, a.app, a.models.split(","), a.rpm, a.tpm, a.daily_budget, a.monthly_budget,
            [t for t in a.tools.split(",") if t], ttl, [n for n in a.cidrs.split(",") if n],
            tool_rpm=a.tool_rpm)
        print(f"id={k.id}  name={k.name}  expires={k.expires_at or 'never'} UTC"
              f"\nkey (shown once): {raw}")
    elif a.cmd == "list-keys":
        for k, hint in store.list():
            print(f"{k.id:>3}  {k.name:<16} team={k.team or '-':<8} ...{hint}  rpm={k.rpm} tpm={k.tpm}"
                  f"  daily=${k.daily_budget_usd}  monthly=${k.monthly_budget_usd}"
                  f"  expires={k.expires_at or 'never'}"
                  f"{'  from=' + ','.join(k.allowed_cidrs) if k.allowed_cidrs else ''}"
                  f"{'  rotated-from=#' + str(k.lineage) if k.lineage else ''}{_state(k)}")
    elif a.cmd == "revoke-key":
        print("revoked" if store.revoke(a.id) else "no such key")
    elif a.cmd == "revoke":
        if not (a.team or a.created_before or a.all):
            ap.error("revoke needs --team, --created-before or --all")
        print(f"revoked {store.revoke_many(a.team, a.created_before, a.all)} key(s)")
    elif a.cmd == "rotate-key":
        out = store.rotate(a.id, parse_duration(a.grace),
                           parse_duration(a.ttl) if a.ttl else None, default_ttl)
        if out is None:
            print("no such key, or it is revoked")
            return
        raw, k = out
        old = store.get(a.id)
        print(f"id={k.id}  name={k.name}  expires={k.expires_at or 'never'} UTC"
              f"\nkey (shown once): {raw}\nold key #{a.id} works until {old.expires_at} UTC"
              f"{_state(old)}")
    elif a.cmd == "suspend-key":
        print("suspended" if store.suspend(a.id, a.reason) else "no such key, or it isn't active")
    elif a.cmd == "unsuspend-key":
        print("active" if store.unsuspend(a.id) else "no such key, or it isn't suspended")
    elif a.cmd == "lockdown":
        scope = f"team {a.team}" if a.team else "all teams"
        if a.lift:
            n = store.unsuspend_many("lockdown", a.team)
            if not a.team:
                store.set_flag("paid_off", None)
            print(f"lockdown lifted for {scope}: {n} key(s) active again"
                  + ("; paid providers follow SLUICE_ALLOW_PAID again" if not a.team else ""))
        else:
            n = store.suspend_many("lockdown", a.team)
            if not a.team:
                store.set_flag("paid_off", "lockdown")
            print(f"lockdown for {scope}: {n} key(s) suspended"
                  + ("; paid providers off" if not a.team else "")
                  + ". Next: revoke the provider keys at the provider (docs/runbook-key-compromise.md)")


def tools(pins, a) -> None:
    if a.tcmd == "list":
        for r in pins.rows():
            flags = json.loads(r["flags"])
            print(f"{r['tool']:<32} {r['status']:<12} {r['seen_sha256'][:12]}"
                  f"  changed={r['changed_at'] or '-'}{'  flags: ' + '; '.join(flags) if flags else ''}")
        return
    r = pins.get(a.tool)
    if r is None:
        print("no such tool (it is pinned when the gateway first lists it)")
        return
    if a.tcmd == "diff":
        old, new = (json.dumps(json.loads(r[c]), indent=2, ensure_ascii=False).splitlines()
                    for c in ("pinned_definition", "seen_definition"))
        print(f"{a.tool}: {r['status']}\n" + ("\n".join(difflib.unified_diff(
            old, new, f"trusted {r['pinned_sha256'][:12]}", f"seen {r['seen_sha256'][:12]}",
            lineterm="")) or "(no change since it was trusted)"))
        for f in json.loads(r["flags"]):
            print(f"lint: {f}")
        print(f"approve with: python -m sluice.admin tools approve {a.tool} "
              f"--sha {r['seen_sha256'][:12]}")
    elif a.tcmd == "approve":
        sha = pins.approve(a.tool, a.sha)
        print(f"approved {a.tool} at {sha[:12]}; replicas serve it after their next tool refresh"
              if sha else "not approved: the definition changed since that sha; diff it again")


if __name__ == "__main__":
    main()
