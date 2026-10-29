"""MCP audit tamper evidence and retention (ADR 0012): editing or deleting a row breaks the
hash chain; retention deletes old rows without breaking it; old databases are chained once."""
import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from sluice import admin, report
from sluice.mcp.audit import GENESIS, McpAudit


async def filled(path, n=5) -> McpAudit:
    a = McpAudit(path)
    for i in range(n):
        await a.record(key_name="k", tool=f"t__{i}", outcome="ok", latency_ms=1.5 * i,
                       args_sha256="ab" * 32, args_bytes=i)
    return a


async def test_an_untouched_chain_verifies(tmp_path):
    a = await filled(str(tmp_path / "a.db"))
    v = a.verify()
    assert v["ok"] and v["rows"] == 5 and v["head"] == a.head() != GENESIS


@pytest.mark.parametrize("tamper,why", [
    ("UPDATE mcp_audit SET outcome='denied' WHERE id=3", "edited"),
    ("DELETE FROM mcp_audit WHERE id=3", "deleted or changed"),
    ("UPDATE mcp_audit SET key_name='someone-else' WHERE id=5", "edited"),
])
async def test_edits_and_deletions_break_it(tmp_path, tamper, why):
    path = str(tmp_path / "a.db")
    await filled(path)
    con = sqlite3.connect(path)
    con.execute(tamper)
    con.commit()
    con.close()
    v = McpAudit(path).verify()
    assert not v["ok"] and why in v["why"]


async def test_retention_keeps_the_rest_verifiable(tmp_path):
    path = str(tmp_path / "a.db")
    a = await filled(path, 6)
    con = sqlite3.connect(path)  # make the first four rows old, then re-chain them as written
    old = (datetime.now(UTC) - timedelta(days=40)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    con.execute("UPDATE mcp_audit SET ts=? WHERE id<=4", (old,))
    con.commit()
    con.close()
    a._db.execute("UPDATE mcp_audit SET prev_hash=NULL, row_hash=NULL")
    with a._lock:
        a._tx(a._chain_unchained)
    assert a.verify()["ok"]
    assert a.prune(30) == 4 and a.prune(30) == 0
    v = a.verify()
    assert v["ok"] and v["rows"] == 2
    await a.record(key_name="k", tool="t__new", outcome="ok")  # chain continues after pruning
    assert a.verify()["ok"] and a.verify()["rows"] == 3
    a.prune(0, now=datetime.now(UTC) + timedelta(seconds=1))  # everything; next row chains on
    await a.record(key_name="k", tool="t__after", outcome="ok")
    assert a.verify() | {"head": None} == {"ok": True, "rows": 1, "bad_id": None, "why": None,
                                           "head": None}


async def test_a_pre_chain_database_is_chained_on_open(tmp_path):
    path = str(tmp_path / "old.db")
    con = sqlite3.connect(path)
    con.execute("""CREATE TABLE mcp_audit (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL,
        trace_id TEXT, key_id INTEGER, key_name TEXT, team TEXT, tool TEXT NOT NULL,
        outcome TEXT NOT NULL, args_sha256 TEXT, args_bytes INTEGER, latency_ms REAL,
        error TEXT)""")
    con.executemany("INSERT INTO mcp_audit (ts, tool, outcome) VALUES (?, ?, 'ok')",
                    [("2026-10-01T00:00:00.000Z", f"t__{i}") for i in range(3)])
    con.commit()
    con.close()
    a = McpAudit(path)
    await a.record(key_name="k", tool="t__new", outcome="ok")
    assert a.verify()["ok"] and a.verify()["rows"] == 4


async def test_two_writers_on_one_file_dont_fork_the_chain(tmp_path):
    path = str(tmp_path / "a.db")
    a, b = McpAudit(path), McpAudit(path)  # two replicas sharing /data/sluice.db
    await asyncio.gather(*[(a if i % 2 else b).record(tool=f"t__{i}", outcome="ok")
                           for i in range(40)])
    assert a.verify() | {"head": None} == {"ok": True, "rows": 40, "bad_id": None, "why": None,
                                           "head": None}


async def test_cli_verify_and_prune(tmp_path, capsys):
    path = str(tmp_path / "a.db")
    await filled(path)
    with pytest.raises(SystemExit) as e:
        report.main(["--db", path, "--verify-audit"])
    assert e.value.code == 0 and "5 rows chain correctly" in capsys.readouterr().out
    admin.main(["--db", path, "prune-audit", "--days", "0"])
    assert "deleted 5 audit row(s)" in capsys.readouterr().out
    con = sqlite3.connect(path)
    con.execute("INSERT INTO mcp_audit (ts, tool, outcome) VALUES ('x', 'forged__row', 'ok')")
    con.commit()
    con.close()
    with pytest.raises(SystemExit) as e:
        report.main(["--db", path, "--verify-audit"])
    assert e.value.code == 1 and "BROKEN" in capsys.readouterr().out


async def test_report_reads_the_audit_path_from_the_policy(tmp_path, capsys):
    audit = str(tmp_path / "audit.db")
    await filled(audit, 2)
    policy = tmp_path / "p.yaml"
    policy.write_text(f"auth: {{db_path: {tmp_path / 'keys.db'}}}\nmcp: {{audit_db: {audit}}}\n"
                      "providers: {mock: {type: mock}}\n")
    with pytest.raises(SystemExit):
        report.main(["--policy", str(policy), "--verify-audit"])
    assert "2 rows chain correctly" in capsys.readouterr().out
