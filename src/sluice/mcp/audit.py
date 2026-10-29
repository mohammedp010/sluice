"""Audit log of every MCP tool call through the gateway, allowed or not.

Arguments are not stored, only their SHA-256 and size: tool arguments routinely carry file
contents, queries and credentials. The hash still answers "was this exact call made, and by
whom" when you have the arguments in hand. Writes run on a worker thread, never on the event
loop (ADR 0007).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import threading
from datetime import UTC, datetime

COLUMNS = ("ts", "trace_id", "key_id", "key_name", "team", "tool", "outcome", "args_sha256",
           "args_bytes", "latency_ms", "error")


def args_digest(args: dict) -> tuple[str, int]:
    raw = json.dumps(args, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest(), len(raw)


class McpAudit:
    def __init__(self, path: str):
        self._db = sqlite3.connect(path, check_same_thread=False, timeout=5)
        self._lock = threading.Lock()
        with self._lock, self._db:
            self._db.execute(
                """CREATE TABLE IF NOT EXISTS mcp_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, trace_id TEXT,
                    key_id INTEGER, key_name TEXT, team TEXT, tool TEXT NOT NULL,
                    outcome TEXT NOT NULL, args_sha256 TEXT, args_bytes INTEGER,
                    latency_ms REAL, error TEXT)""")

    def _write(self, row: dict) -> None:
        with self._lock, self._db:
            self._db.execute(
                f"INSERT INTO mcp_audit ({','.join(COLUMNS)}) "  # noqa: S608 -- constant columns
                f"VALUES ({','.join('?' * len(COLUMNS))})", tuple(row.get(c) for c in COLUMNS))

    async def record(self, **row) -> None:
        now = datetime.now(UTC)
        row["ts"] = now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"
        await asyncio.to_thread(self._write, row)

    def rows(self, limit: int = 100) -> list[dict]:
        with self._lock:
            cur = self._db.execute("SELECT * FROM mcp_audit ORDER BY id DESC LIMIT ?", (limit,))
            names = [d[0] for d in cur.description]
            return [dict(zip(names, r)) for r in cur.fetchall()]

    def summary(self) -> list[tuple]:
        """(key, tool, outcome, calls)"""
        with self._lock:
            return [tuple(r) for r in self._db.execute(
                "SELECT key_name, tool, outcome, COUNT(*) FROM mcp_audit "
                "GROUP BY 1, 2, 3 ORDER BY 1, 2, 3").fetchall()]
