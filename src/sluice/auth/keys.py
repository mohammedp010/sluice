"""Virtual keys: what clients present to Sluice. They never map to raw provider credentials.

Only a SHA-256 hash of the key is stored. A plain fast hash is fine here (unlike passwords):
the key is 256 bits of randomness, so there is nothing to brute-force.

Containment (ADR 0011): a key can expire, be bound to networks, be suspended (reversible) or
revoked (final), and be rotated with a grace period. Every way of failing the check looks the
same to the caller (401); `check()` returns the real reason for telemetry.
"""
from __future__ import annotations

import calendar
import fnmatch
import hashlib
import ipaddress
import json
import re
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass, field

PREFIX = "sk-sluice-"
TS = "%Y-%m-%d %H:%M:%S"  # UTC, the format of sqlite's CURRENT_TIMESTAMP, so text sorts by time
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(s: str) -> int:
    """'90d', '24h', '30m', '45s' -> seconds."""
    m = re.fullmatch(r"(\d+)([smhd])", s.strip())
    if not m:
        raise ValueError(f"bad duration {s!r}: use a number and s, m, h or d (e.g. 30d)")
    return int(m[1]) * _UNITS[m[2]]


def _ts(t: float) -> str:
    return time.strftime(TS, time.gmtime(t))


def _epoch(ts: str) -> float:
    return calendar.timegm(time.strptime(ts, TS))


def parse_networks(cidrs: list[str]) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    return [ipaddress.ip_network(c.strip(), strict=False) for c in cidrs]


def client_ip(peer: str | None, forwarded_for: str | None, trusted: list) -> str | None:
    """The caller's address. X-Forwarded-For counts only when the peer is a trusted proxy, and
    is read right to left, skipping trusted hops: entries further left are the client's to
    write, so the first untrusted hop is the last one we can believe."""
    try:
        ip = ipaddress.ip_address(peer or "")
    except ValueError:
        return peer
    hops = [h.strip() for h in (forwarded_for or "").split(",") if h.strip()]
    while hops and any(ip in n for n in trusted):
        try:
            ip = ipaddress.ip_address(hops.pop())
        except ValueError:
            return None  # a trusted proxy passed on garbage: no address we can bind to
    return str(ip)


@dataclass
class VirtualKey:
    id: int
    name: str
    team: str = ""
    app: str = ""
    allowed_models: list[str] = field(default_factory=lambda: ["*"])
    # MCP tools this key may see and call, as "server__tool" globs. Empty: none (deny by default)
    allowed_tools: list[str] = field(default_factory=list)
    rpm: int = 60
    tpm: int = 20_000
    daily_budget_usd: float | None = None
    monthly_budget_usd: float | None = None
    status: str = "active"  # active | suspended | revoked
    suspended_reason: str | None = None
    expires_at: str | None = None  # UTC, TS format; None: never
    allowed_cidrs: list[str] = field(default_factory=list)  # empty: any network
    # The key this one was rotated from, at the root of the chain. Limits and budgets count per
    # lineage, so a rotation's grace overlap doesn't double them or reset today's spend.
    lineage: int | None = None
    created_at: str = ""

    @property
    def revoked(self) -> bool:
        return self.status == "revoked"

    @property
    def limit_id(self) -> int:
        return self.lineage or self.id

    def allows(self, model: str) -> bool:
        return "*" in self.allowed_models or model in self.allowed_models

    def allows_tool(self, name: str) -> bool:
        return any(fnmatch.fnmatchcase(name, pattern) for pattern in self.allowed_tools)


def hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


class KeyStore:
    def __init__(self, path: str = "sluice.db"):
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock, self._db:
            self._db.execute(
                """CREATE TABLE IF NOT EXISTS keys (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    key_hash TEXT UNIQUE NOT NULL,
                    key_hint TEXT NOT NULL,
                    name TEXT NOT NULL, team TEXT NOT NULL DEFAULT '', app TEXT NOT NULL DEFAULT '',
                    allowed_models TEXT NOT NULL DEFAULT '["*"]',
                    rpm INTEGER NOT NULL, tpm INTEGER NOT NULL,
                    daily_budget_usd REAL, monthly_budget_usd REAL,
                    revoked INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"""
            )
            cols = {r[1] for r in self._db.execute("PRAGMA table_info(keys)")}
            if "allowed_tools" not in cols:  # databases created before Phase 8
                self._db.execute(
                    "ALTER TABLE keys ADD COLUMN allowed_tools TEXT NOT NULL DEFAULT '[]'")
            if "status" not in cols:  # before containment (ADR 0011)
                for ddl in ("status TEXT NOT NULL DEFAULT 'active'", "suspended_reason TEXT",
                            "expires_at TEXT", "allowed_cidrs TEXT NOT NULL DEFAULT '[]'",
                            "lineage INTEGER"):
                    self._db.execute(f"ALTER TABLE keys ADD COLUMN {ddl}")
                self._db.execute("UPDATE keys SET status='revoked' WHERE revoked=1")

    @staticmethod
    def _row(r: sqlite3.Row) -> VirtualKey:
        return VirtualKey(
            id=r["id"], name=r["name"], team=r["team"], app=r["app"],
            allowed_models=json.loads(r["allowed_models"]), rpm=r["rpm"], tpm=r["tpm"],
            allowed_tools=json.loads(r["allowed_tools"]),
            daily_budget_usd=r["daily_budget_usd"], monthly_budget_usd=r["monthly_budget_usd"],
            status=r["status"], suspended_reason=r["suspended_reason"],
            expires_at=r["expires_at"], allowed_cidrs=json.loads(r["allowed_cidrs"]),
            lineage=r["lineage"], created_at=r["created_at"],
        )

    def create(
        self, name: str, team: str = "", app: str = "", allowed_models: list[str] | None = None,
        rpm: int = 60, tpm: int = 20_000, daily_budget_usd: float | None = None,
        monthly_budget_usd: float | None = None, allowed_tools: list[str] | None = None,
        ttl_s: int | None = None, allowed_cidrs: list[str] | None = None,
        lineage: int | None = None, now: float | None = None,
    ) -> tuple[str, VirtualKey]:
        """Returns (raw_key, record). The raw key is shown once and never stored."""
        now = time.time() if now is None else now
        cidrs = [str(n) for n in parse_networks(allowed_cidrs or [])]  # refuse typos now
        raw = PREFIX + secrets.token_urlsafe(32)
        with self._lock, self._db:
            cur = self._db.execute(
                "INSERT INTO keys (key_hash,key_hint,name,team,app,allowed_models,rpm,tpm,"
                "daily_budget_usd,monthly_budget_usd,allowed_tools,expires_at,allowed_cidrs,"
                "lineage,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (hash_key(raw), raw[-4:], name, team, app, json.dumps(allowed_models or ["*"]),
                 rpm, tpm, daily_budget_usd, monthly_budget_usd,
                 json.dumps(allowed_tools or []), _ts(now + ttl_s) if ttl_s else None,
                 json.dumps(cidrs), lineage, _ts(now)),
            )
            row = self._db.execute("SELECT * FROM keys WHERE id=?", (cur.lastrowid,)).fetchone()
        return raw, self._row(row)

    def check(self, raw: str, ip: str | None = None,
              now: float | None = None) -> tuple[VirtualKey | None, str]:
        """(key, "ok") or (None, why): unknown, revoked, suspended, expired or network."""
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM keys WHERE key_hash=?", (hash_key(raw),)).fetchone()
        if row is None:
            return None, "unknown"
        k = self._row(row)
        if k.status != "active":
            return None, k.status
        if k.expires_at and k.expires_at <= _ts(time.time() if now is None else now):
            return None, "expired"
        if k.allowed_cidrs:
            try:
                addr = ipaddress.ip_address(ip or "")
            except ValueError:
                return None, "network"
            if not any(addr in n for n in parse_networks(k.allowed_cidrs)):
                return None, "network"
        return k, "ok"

    def lookup(self, raw: str, ip: str | None = None) -> VirtualKey | None:
        return self.check(raw, ip)[0]

    def get(self, key_id: int) -> VirtualKey | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM keys WHERE id=?", (key_id,)).fetchone()
        return self._row(row) if row else None

    def revoke(self, key_id: int) -> bool:
        # `revoked` is kept in step so an older build reading this database still refuses it
        with self._lock, self._db:
            return self._db.execute(
                "UPDATE keys SET status='revoked', revoked=1 WHERE id=?", (key_id,)).rowcount > 0

    def revoke_many(self, team: str | None = None, created_before: str | None = None,
                    everything: bool = False) -> int:
        """Revoke every live key matching all the filters given. Returns how many."""
        if team is None and created_before is None and not everything:
            raise ValueError("give a team, a created-before time, or everything=True")
        with self._lock, self._db:
            return self._db.execute(
                "UPDATE keys SET status='revoked', revoked=1 WHERE status!='revoked'"
                " AND (? IS NULL OR team=?) AND (? IS NULL OR created_at<?)",
                (team, team, created_before, created_before)).rowcount

    def suspend(self, key_id: int, reason: str) -> bool:
        """Reversible: `unsuspend` restores the key as it was. A revoked key stays revoked."""
        with self._lock, self._db:
            return self._db.execute(
                "UPDATE keys SET status='suspended', suspended_reason=? "
                "WHERE id=? AND status!='revoked'", (reason, key_id)).rowcount > 0

    def unsuspend(self, key_id: int) -> bool:
        with self._lock, self._db:
            return self._db.execute(
                "UPDATE keys SET status='active', suspended_reason=NULL "
                "WHERE id=? AND status='suspended'", (key_id,)).rowcount > 0

    def rotate(self, key_id: int, grace_s: int, ttl_s: int | None = None,
               default_ttl_s: int | None = None,
               now: float | None = None) -> tuple[str, VirtualKey] | None:
        """Issue a new key with the same grants; the old one keeps working for `grace_s` more
        (never longer than it would have anyway). The new key's lifetime is `ttl_s`, else the
        old key's, else `default_ttl_s`. A revoked key can't be rotated; a suspended one can,
        and stays suspended (that's the incident case: new secret for a service you trust)."""
        now = time.time() if now is None else now
        old = self.get(key_id)
        if old is None or old.revoked:
            return None
        if ttl_s is None and old.expires_at:
            ttl_s = int(_epoch(old.expires_at) - _epoch(old.created_at))
        raw, new = self.create(
            old.name, old.team, old.app, old.allowed_models, old.rpm, old.tpm,
            old.daily_budget_usd, old.monthly_budget_usd, old.allowed_tools,
            ttl_s or default_ttl_s, old.allowed_cidrs, old.limit_id, now)
        end = _ts(now + grace_s)
        with self._lock, self._db:
            self._db.execute(
                "UPDATE keys SET expires_at=? WHERE id=? AND (expires_at IS NULL OR expires_at>?)",
                (end, key_id, end))
        return raw, new

    def list(self) -> list[tuple[VirtualKey, str]]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM keys ORDER BY id").fetchall()
        return [(self._row(r), r["key_hint"]) for r in rows]
