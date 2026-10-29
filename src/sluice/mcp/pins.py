"""Tool pinning (ADR 0011): a tool definition, once trusted, can't change without a review.

MCP clients apply tool metadata as soon as a server sends it, so a server can pass review with
an honest description and swap in a poisoned one later (a "rug pull"). Here every definition
the gateway relays is hashed as canonical JSON and pinned on first sight. If a later refresh
sees a different hash, the tool is quarantined: hidden from tools/list, refused on tools/call
like an unknown tool, until `sluice.admin tools approve` re-pins it.

First sight: `auto_first` trusts it (unless the lint below flags it), `manual` holds every new
tool as pending. The lint is a cheap first filter, not a defence: poisoning can be phrased
innocently. Pins live in sqlite next to the MCP audit, so replicas and the CLI share them; a
replica re-reads a tool's pin each time it refreshes that server's tool list.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import threading
import time

from mcp.types import Tool
from opentelemetry import trace

log = logging.getLogger("sluice.security")

# zero-width and joiners, bidi embeddings/overrides/isolates, word joiner etc., BOM, tag block
_INVISIBLE = re.compile("[\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff"
                        "\U000e0000-\U000e007f]")
_INSTRUCTION = re.compile(
    r"ignore\s+(?:all\s+|any\s+)?(?:the\s+)?(?:previous|prior|above|earlier|other)"
    r"|<\s*/?\s*(?:important|system|instructions?|secret|hidden)\b"
    r"|(?:do\s+not|don'?t|never)\s+(?:tell|inform|mention|reveal|notify|show|let)\b[^.]{0,40}\buser"
    r"|\b(?:before|prior\s+to)\s+(?:using|calling|invoking)\s+(?:this|any|the)\s+(?:tool|function)"
    r"|\bsystem\s+prompt\b|\bprevious\s+instructions\b|\bsidenote\b"
    r"|\bthis\s+is\s+(?:very\s+)?important\b",
    re.IGNORECASE)
_SENSITIVE = re.compile(
    r"~/\.|\.ssh\b|\bid_(?:rsa|ed25519|ecdsa)\b|(?<![\w.])\.env\b|\.aws/|\.npmrc|\.pypirc"
    r"|mcp\.json|/etc/(?:passwd|shadow)|\.git-credentials|\.kube/config|\bcredentials\b"
    r"|private[ _]key",
    re.IGNORECASE)
_URL = re.compile(r"https?://|\bwww\.", re.IGNORECASE)
_PUBLIC_REF = re.compile(r"\b\w+__\w+\b")  # a gateway-style "server__tool" name


def definition(tool: Tool) -> dict:
    """What the gateway relays to clients, before it renames the tool."""
    return tool.model_dump(mode="json", exclude_none=True, by_alias=True)


def digest(d: dict) -> str:
    raw = json.dumps(d, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def _strings(x) -> list[str]:
    """Every string in a definition, keys included: parameter names carry instructions too."""
    if isinstance(x, str):
        return [x]
    if isinstance(x, dict):
        return [s for k, v in x.items() for s in (*_strings(k), *_strings(v))]
    if isinstance(x, list):
        return [s for v in x for s in _strings(v)]
    return []


def lint(d: dict, other_tools: set[str] = frozenset(), max_description: int = 1024) -> list[str]:
    """Reasons a definition looks like tool poisoning, as "kind: snippet". Empty: none seen.

    other_tools: upstream names of tools on *other* servers. Only distinctive ones (with _ or -)
    count, so a description saying "search" doesn't trip on a tool called search."""
    flags: list[str] = []
    text = "\n".join(_strings({k: v for k, v in d.items() if k != "name"}))

    def add(kind: str, m: re.Match | None) -> None:
        if m:
            flags.append(f"{kind}: {m.group(0)!r}"[:80])

    m = _INVISIBLE.search(text)
    if m:
        flags.append(f"invisible: U+{ord(m.group(0)):04X}")
    add("instruction", _INSTRUCTION.search(text))
    add("sensitive_path", _SENSITIVE.search(text))
    add("url", _URL.search(text))
    add("other_tool", _PUBLIC_REF.search(text))
    for name in sorted(n for n in other_tools if ("_" in n or "-" in n) and n != d.get("name")):
        add("other_tool", re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", text))
    if len(d.get("description") or "") > max_description:
        flags.append(f"long: {len(d['description'])} chars > {max_description}")
    return flags


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())


class ToolPins:
    """Pin state per public tool name. `status()` is answered from memory on the request path;
    the database is touched only when a server's tool list is refreshed."""

    def __init__(self, path: str, mode: str = "auto_first", max_description: int = 1024):
        self.mode, self.max_description = mode, max_description
        self._db = sqlite3.connect(path, check_same_thread=False, timeout=5)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._status: dict[str, str] = {}
        with self._lock, self._db:
            # pinned_* is the trusted definition, seen_* the latest one a server sent. A tool is
            # served only when status is approved, which implies the two are the same.
            self._db.execute(
                """CREATE TABLE IF NOT EXISTS mcp_tool_pins (
                    tool TEXT PRIMARY KEY, status TEXT NOT NULL,
                    pinned_sha256 TEXT NOT NULL, pinned_definition TEXT NOT NULL,
                    seen_sha256 TEXT NOT NULL, seen_definition TEXT NOT NULL,
                    flags TEXT NOT NULL DEFAULT '[]',
                    first_seen TEXT NOT NULL, changed_at TEXT, approved_at TEXT)""")

    def status(self, public: str) -> str | None:
        return self._status.get(public)

    def observe(self, public: str, d: dict, other_tools: set[str] = frozenset()) -> str:
        """Record the definition a refresh returned; returns the tool's status after it."""
        sha = digest(d)
        with self._lock, self._db:
            row = self._db.execute(
                "SELECT * FROM mcp_tool_pins WHERE tool=?", (public,)).fetchone()
            if row is None:
                flags = lint(d, other_tools, self.max_description)
                status = "approved" if self.mode == "auto_first" and not flags else "pending"
                defn, now = json.dumps(d, sort_keys=True), _now()
                # OR IGNORE: another replica may pin it at the same moment; its row wins
                self._db.execute(
                    "INSERT OR IGNORE INTO mcp_tool_pins (tool,status,pinned_sha256,"
                    "pinned_definition,seen_sha256,seen_definition,flags,first_seen,approved_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    (public, status, sha, defn, sha, defn, json.dumps(flags), now,
                     now if status == "approved" else None))
                row = self._db.execute(
                    "SELECT * FROM mcp_tool_pins WHERE tool=?", (public,)).fetchone()
                if row["status"] == "pending":
                    self._event("pending", public, flags)
            elif sha != row["seen_sha256"]:
                # A change. Approved -> quarantined; pending stays pending, and approving it
                # later approves what was seen last. Quarantine sticks even if the server
                # changes back: it has shown it will change under you.
                flags = lint(d, other_tools, self.max_description)
                status = "pending" if row["status"] == "pending" else "quarantined"
                self._db.execute(
                    "UPDATE mcp_tool_pins SET status=?, seen_sha256=?, seen_definition=?,"
                    " flags=?, changed_at=? WHERE tool=?",
                    (status, sha, json.dumps(d, sort_keys=True), json.dumps(flags), _now(),
                     public))
                if status == "quarantined":
                    self._event("quarantined", public, flags, row["pinned_sha256"], sha)
                row = {"status": status}
        self._status[public] = row["status"]
        return row["status"]

    def _event(self, what: str, public: str, flags: list[str], old: str = "",
               new: str = "") -> None:
        attrs = {"gen_ai.tool.name": public, "sluice.mcp.flags": flags}
        if old:
            attrs |= {"sluice.mcp.pinned_sha256": old, "sluice.mcp.seen_sha256": new}
        trace.get_current_span().add_event(f"sluice.mcp.tool_{what}", attrs)
        log.warning("mcp tool %s: %s %s%s", what, public,
                    f"(definition changed: {old[:12]} -> {new[:12]}) " if old else "", flags)

    def approve(self, public: str, sha_prefix: str | None = None) -> str | None:
        """Pin what was seen last. With `sha_prefix`, only if that is still what was seen, so
        a definition that changed after you reviewed the diff isn't approved by accident.
        Returns the approved sha256, or None."""
        with self._lock, self._db:
            row = self._db.execute(
                "SELECT seen_sha256 FROM mcp_tool_pins WHERE tool=?", (public,)).fetchone()
            if row is None or (sha_prefix and not row[0].startswith(sha_prefix)):
                return None
            self._db.execute(
                "UPDATE mcp_tool_pins SET status='approved', pinned_sha256=seen_sha256,"
                " pinned_definition=seen_definition, approved_at=? WHERE tool=?",
                (_now(), public))
        return row[0]

    def get(self, public: str) -> dict | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM mcp_tool_pins WHERE tool=?", (public,)).fetchone()
        return dict(row) if row else None

    def rows(self) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._db.execute(
                "SELECT tool, status, pinned_sha256, seen_sha256, flags, first_seen, changed_at,"
                " approved_at FROM mcp_tool_pins ORDER BY tool")]
