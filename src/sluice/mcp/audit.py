"""Audit log of every MCP tool call through the gateway, allowed or not.

Arguments are not stored, only their SHA-256 and size: tool arguments routinely carry file
contents, queries and credentials. The hash still answers "was this exact call made, and by
whom" when you have the arguments in hand. Writes run on a worker thread, never on the event
loop (ADR 0007).

Tamper evidence (ADR 0012): each row stores the previous row's hash and its own, sha256 over
the previous hash and the row as canonical JSON. `verify()` walks the chain: an edited row, or
one deleted from the middle, breaks it. Retention deletes old rows and records a checkpoint (the
last deleted row's hash), so what remains still verifies. The chain lives in the same database,
so someone who can write to it can rebuild the whole chain; keep the `head()` hash somewhere
else (a log, a ticket) to catch that.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import threading
from datetime import UTC, datetime, timedelta

COLUMNS = ("ts", "trace_id", "key_id", "key_name", "team", "tool", "outcome", "args_sha256",
           "args_bytes", "latency_ms", "error", "egress")
GENESIS = "0" * 64


def chain_hash(prev: str, row) -> str:
    """row: a mapping with every column in COLUMNS."""
    body = json.dumps([row[c] for c in COLUMNS], separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256((prev + body).encode()).hexdigest()


def args_digest(args: dict) -> tuple[str, int]:
    raw = json.dumps(args, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest(), len(raw)


def _ts(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"


def _ts_now() -> str:
    return _ts(datetime.now(UTC))


class McpAudit:
    def __init__(self, path: str):
        # autocommit: writes take BEGIN IMMEDIATE themselves, so two replicas writing one file
        # can't both read the same chain head and fork it
        self._db = sqlite3.connect(path, check_same_thread=False, timeout=5,
                                   isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._db.execute(
                """CREATE TABLE IF NOT EXISTS mcp_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, trace_id TEXT,
                    key_id INTEGER, key_name TEXT, team TEXT, tool TEXT NOT NULL,
                    outcome TEXT NOT NULL, args_sha256 TEXT, args_bytes INTEGER,
                    latency_ms REAL, error TEXT)""")
            cols = {r[1] for r in self._db.execute("PRAGMA table_info(mcp_audit)")}
            if "egress" not in cols:  # before the egress guard (ADR 0012)
                # secrets seen in arguments or results: JSON list of type + position, no values
                self._db.execute("ALTER TABLE mcp_audit ADD COLUMN egress TEXT")
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS mcp_audit_checkpoints (pruned_through INTEGER NOT NULL,"
                " last_hash TEXT NOT NULL, rows INTEGER NOT NULL, ts TEXT NOT NULL)")
            if "row_hash" not in cols:  # before the hash chain: chain what's there, once
                self._db.execute("ALTER TABLE mcp_audit ADD COLUMN prev_hash TEXT")
                self._db.execute("ALTER TABLE mcp_audit ADD COLUMN row_hash TEXT")
                self._tx(self._chain_unchained)

    def _tx(self, fn, *args):
        """Run fn inside BEGIN IMMEDIATE ... COMMIT (the caller holds self._lock)."""
        self._db.execute("BEGIN IMMEDIATE")
        try:
            out = fn(*args)
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        self._db.execute("COMMIT")
        return out

    def _head(self) -> str:
        row = self._db.execute(
            "SELECT row_hash FROM mcp_audit WHERE row_hash IS NOT NULL ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row:
            return row[0]
        cp = self._db.execute(
            "SELECT last_hash FROM mcp_audit_checkpoints ORDER BY rowid DESC LIMIT 1").fetchone()
        return cp[0] if cp else GENESIS

    def _chain_unchained(self) -> None:
        prev = GENESIS
        for r in self._db.execute("SELECT * FROM mcp_audit ORDER BY id").fetchall():
            h = chain_hash(prev, r)
            self._db.execute("UPDATE mcp_audit SET prev_hash=?, row_hash=? WHERE id=?",
                             (prev, h, r["id"]))
            prev = h

    def _insert(self, row: dict) -> None:
        prev = self._head()
        values = {c: row.get(c) for c in COLUMNS}
        cols = (*COLUMNS, "prev_hash", "row_hash")
        self._db.execute(
            f"INSERT INTO mcp_audit ({','.join(cols)}) "  # noqa: S608 -- constant columns
            f"VALUES ({','.join('?' * len(cols))})",
            (*values.values(), prev, chain_hash(prev, values)))

    def _write(self, row: dict) -> None:
        with self._lock:
            self._tx(self._insert, row)

    def head(self) -> str:
        """The newest row's hash. Record it outside this database to make rewriting evident."""
        with self._lock:
            return self._head()

    def verify(self) -> dict:
        """Walk the chain. {"ok", "rows", "bad_id", "why"}; bad_id is the first row that fails."""
        with self._lock:
            cp = self._db.execute("SELECT last_hash FROM mcp_audit_checkpoints "
                                  "ORDER BY rowid DESC LIMIT 1").fetchone()
            rows = self._db.execute("SELECT * FROM mcp_audit ORDER BY id").fetchall()
        prev, n = (cp[0] if cp else GENESIS), 0
        for r in rows:
            if r["prev_hash"] != prev:
                return {"ok": False, "rows": n, "bad_id": r["id"],
                        "why": "links to a different previous row (one was deleted or changed)"}
            if chain_hash(prev, r) != r["row_hash"]:
                return {"ok": False, "rows": n, "bad_id": r["id"], "why": "row was edited"}
            prev, n = r["row_hash"], n + 1
        return {"ok": True, "rows": n, "bad_id": None, "why": None, "head": prev}

    def prune(self, older_than_days: float, now: datetime | None = None) -> int:
        """Retention: delete rows older than the cut-off and checkpoint the last one's hash, so
        the rows that remain still verify. Returns how many were deleted."""
        cutoff = _ts((now or datetime.now(UTC)) - timedelta(days=older_than_days))

        def run() -> int:
            last = self._db.execute("SELECT id, row_hash FROM mcp_audit WHERE ts<? "
                                    "ORDER BY id DESC LIMIT 1", (cutoff,)).fetchone()
            if last is None:
                return 0
            n = self._db.execute("DELETE FROM mcp_audit WHERE id<=?", (last["id"],)).rowcount
            self._db.execute("INSERT INTO mcp_audit_checkpoints VALUES (?,?,?,?)",
                             (last["id"], last["row_hash"], n, _ts_now()))
            return n

        with self._lock:
            return self._tx(run)

    async def record(self, **row) -> None:
        row["ts"] = _ts_now()
        await asyncio.to_thread(self._write, row)

    def rows(self, limit: int = 100) -> list[dict]:
        with self._lock:
            cur = self._db.execute("SELECT * FROM mcp_audit ORDER BY id DESC LIMIT ?", (limit,))
            names = [d[0] for d in cur.description]
            return [dict(zip(names, r)) for r in cur.fetchall()]

    def outcomes(self, since: str | None = None) -> list[tuple]:
        """(key_name, tool, outcome, egress) for every call at or after `since` (a ts prefix)."""
        with self._lock:
            return [tuple(r) for r in self._db.execute(
                "SELECT key_name, tool, outcome, egress FROM mcp_audit "
                "WHERE (? IS NULL OR ts>=?) ORDER BY id", (since, since)).fetchall()]

    def summary(self) -> list[tuple]:
        """(key, tool, outcome, calls)"""
        with self._lock:
            return [tuple(r) for r in self._db.execute(
                "SELECT key_name, tool, outcome, COUNT(*) FROM mcp_audit "
                "GROUP BY 1, 2, 3 ORDER BY 1, 2, 3").fetchall()]
