"""Tiny admin CLI:  python -m sluice.admin create-key --name demo --rpm 30"""
from __future__ import annotations

import argparse

from .auth.keys import KeyStore


def main(argv=None):
    ap = argparse.ArgumentParser(prog="sluice-admin")
    ap.add_argument("--db", default="sluice.db")
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
    sub.add_parser("list-keys")
    r = sub.add_parser("revoke-key")
    r.add_argument("id", type=int)
    a = ap.parse_args(argv)
    store = KeyStore(a.db)

    if a.cmd == "create-key":
        raw, k = store.create(
            a.name, a.team, a.app, a.models.split(","), a.rpm, a.tpm, a.daily_budget, a.monthly_budget)
        print(f"id={k.id}  name={k.name}\nkey (shown once): {raw}")
    elif a.cmd == "list-keys":
        for k, hint in store.list():
            print(f"{k.id:>3}  {k.name:<16} team={k.team or '-':<8} ...{hint}  rpm={k.rpm} tpm={k.tpm}"
                  f"  daily=${k.daily_budget_usd}  monthly=${k.monthly_budget_usd}"
                  f"{'  REVOKED' if k.revoked else ''}")
    elif a.cmd == "revoke-key":
        print("revoked" if store.revoke(a.id) else "no such key")


if __name__ == "__main__":
    main()
