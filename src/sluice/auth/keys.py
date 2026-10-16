"""Virtual keys: what clients present to Sluice. They never map to raw provider credentials.

Only a SHA-256 hash of the key is stored. A plain fast hash is fine here (unlike passwords):
the key is 256 bits of randomness, so there is nothing to brute-force.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import secrets
import sqlite3
import threading
from dataclasses import dataclass, field

PREFIX = "sk-sluice-"


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
    revoked: bool = False

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

    @staticmethod
    def _row(r: sqlite3.Row) -> VirtualKey:
        return VirtualKey(
            id=r["id"], name=r["name"], team=r["team"], app=r["app"],
            allowed_models=json.loads(r["allowed_models"]), rpm=r["rpm"], tpm=r["tpm"],
            allowed_tools=json.loads(r["allowed_tools"]),
            daily_budget_usd=r["daily_budget_usd"], monthly_budget_usd=r["monthly_budget_usd"],
            revoked=bool(r["revoked"]),
        )

    def create(
        self, name: str, team: str = "", app: str = "", allowed_models: list[str] | None = None,
        rpm: int = 60, tpm: int = 20_000, daily_budget_usd: float | None = None,
        monthly_budget_usd: float | None = None, allowed_tools: list[str] | None = None,
    ) -> tuple[str, VirtualKey]:
        """Returns (raw_key, record). The raw key is shown once and never stored."""
        raw = PREFIX + secrets.token_urlsafe(32)
        with self._lock, self._db:
            cur = self._db.execute(
                "INSERT INTO keys (key_hash,key_hint,name,team,app,allowed_models,rpm,tpm,"
                "daily_budget_usd,monthly_budget_usd,allowed_tools) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (hash_key(raw), raw[-4:], name, team, app, json.dumps(allowed_models or ["*"]),
                 rpm, tpm, daily_budget_usd, monthly_budget_usd,
                 json.dumps(allowed_tools or [])),
            )
            row = self._db.execute("SELECT * FROM keys WHERE id=?", (cur.lastrowid,)).fetchone()
        return raw, self._row(row)

    def lookup(self, raw: str) -> VirtualKey | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM keys WHERE key_hash=?", (hash_key(raw),)).fetchone()
        if row is None or row["revoked"]:
            return None
        return self._row(row)

    def revoke(self, key_id: int) -> bool:
        with self._lock, self._db:
            return self._db.execute(
                "UPDATE keys SET revoked=1 WHERE id=?", (key_id,)).rowcount > 0

    def list(self) -> list[tuple[VirtualKey, str]]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM keys ORDER BY id").fetchall()
        return [(self._row(r), r["key_hint"]) for r in rows]
