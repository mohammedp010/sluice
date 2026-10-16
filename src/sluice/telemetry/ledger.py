"""Per-request usage ledger in SQLite: the source for cost attribution by team / app / key.

Metrics can't hold this (one series per key is a cardinality bomb) and traces are sampled and
expire. One durable row per request that reached a provider is cheap and is plain SQL to query.
Team and app are copied onto the row at write time, so spend stays attributed to whoever
owned the key *then*, even if the key is edited later.
"""
from __future__ import annotations

import sqlite3
import threading

COLUMNS = ("trace_id", "key_id", "key_name", "team", "app", "model", "provider",
           "upstream_model", "status", "outcome", "attempts", "fallbacks",
           "prompt_tokens", "completion_tokens", "cost_micro_usd", "latency_ms")

GROUPS = {"team": "team", "app": "app", "key": "key_name", "model": "model",
          "provider": "provider", "day": "substr(ts, 1, 10)"}


class UsageLedger:
    def __init__(self, path: str = "sluice.db"):
        # timeout: two gateway replicas share one file in docker-compose; wait, don't fail.
        # Not WAL: it needs shared memory, which is unreliable on Docker Desktop bind mounts.
        self._db = sqlite3.connect(path, check_same_thread=False, timeout=5)
        self._lock = threading.Lock()
        with self._lock, self._db:
            self._db.execute(
                """CREATE TABLE IF NOT EXISTS usage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                    trace_id TEXT, key_id INTEGER, key_name TEXT, team TEXT, app TEXT,
                    model TEXT, provider TEXT, upstream_model TEXT,
                    status INTEGER, outcome TEXT, attempts INTEGER, fallbacks INTEGER,
                    prompt_tokens INTEGER, completion_tokens INTEGER,
                    cost_micro_usd INTEGER, latency_ms REAL)""")
            self._db.execute("CREATE INDEX IF NOT EXISTS usage_ts ON usage(ts)")

    def record(self, **row) -> None:
        with self._lock, self._db:
            self._db.execute(
                f"INSERT INTO usage ({','.join(COLUMNS)}) VALUES ({','.join('?' * len(COLUMNS))})",
                tuple(row.get(c) for c in COLUMNS))

    def summary(self, by: str = "team", since: str | None = None) -> list[tuple]:
        """(group, requests, errors, fallbacks, input tokens, output tokens, cost USD)."""
        col = GROUPS[by]  # whitelisted: never interpolate user input into SQL
        with self._lock:
            rows = self._db.execute(
                f"""SELECT COALESCE(NULLIF({col}, ''), '-') AS g, COUNT(*),
                           SUM(status >= 400), SUM(fallbacks > 0),
                           SUM(prompt_tokens), SUM(completion_tokens), SUM(cost_micro_usd) / 1e6
                    FROM usage WHERE ts >= ? GROUP BY g ORDER BY 7 DESC, 1""",
                (since or "",)).fetchall()
        return [tuple(r) for r in rows]

    def last(self) -> dict | None:
        return self._one("SELECT * FROM usage ORDER BY id DESC LIMIT 1", ())

    def by_trace(self, trace_id: str) -> dict | None:
        """trace id -> cost row: the link between "what happened" and "what it cost"."""
        return self._one("SELECT * FROM usage WHERE trace_id = ?", (trace_id,))

    def _one(self, sql: str, args: tuple) -> dict | None:
        with self._lock:
            cur = self._db.execute(sql, args)
            r = cur.fetchone()
        return dict(zip([d[0] for d in cur.description], r)) if r else None
