"""Phase 8: MCP proxy. Official SDK client -> Sluice /mcp -> official SDK server (stdio)."""
import json
import sys
from pathlib import Path

import httpx
import pytest
from conftest import ROUTE, client_for, make_app
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import McpError

from sluice.config import Settings
from sluice.mcp.audit import args_digest

SERVER = [sys.executable, str(Path(__file__).parent / "mcp_demo_server.py")]


def mcp_app(tools, servers=None):
    settings = Settings(providers={"mock": {"type": "mock"}}, routes={"m": ROUTE},
                        mcp={"servers": servers or {"demo": {"command": SERVER}},
                             "allowed_origins": ["https://app.example"]})
    return make_app(settings, key_kw={"allowed_tools": tools, "team": "search"})


class sdk_session:
    """The official MCP client, speaking to the ASGI app in-process."""

    def __init__(self, app, raw):
        self.app, self.raw = app, raw

    async def __aenter__(self) -> ClientSession:
        self._http = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),
                                       headers={"Authorization": f"Bearer {self.raw}"})
        self._t = streamable_http_client("http://test/mcp", http_client=self._http)
        read, write, _ = await self._t.__aenter__()
        self._s = ClientSession(read, write)
        session = await self._s.__aenter__()
        await session.initialize()
        return session

    async def __aexit__(self, *exc):
        await self._s.__aexit__(*exc)
        await self._t.__aexit__(*exc)
        await self._http.aclose()
        await self.app.state.mcp.up.close()


async def test_lists_only_granted_tools_and_calls_them():
    app, raw = mcp_app(["demo__add", "demo__echo"])
    async with sdk_session(app, raw) as s:
        names = [t.name for t in (await s.list_tools()).tools]
        assert names == ["demo__add", "demo__echo"]
        r = await s.call_tool("demo__add", {"a": 2, "b": 3})
        assert not r.isError and r.content[0].text == "5"


async def test_denied_looks_exactly_like_unknown_and_both_are_audited():
    app, raw = mcp_app(["demo__add"])
    async with sdk_session(app, raw) as s:
        with pytest.raises(McpError) as denied:
            await s.call_tool("demo__secret", {})
        with pytest.raises(McpError) as unknown:
            await s.call_tool("demo__nope", {})
    msg = lambda e: e.value.error.message.split(":")[0]
    assert msg(denied) == msg(unknown) == "Unknown tool"  # no hint that `secret` exists
    outcomes = {r["tool"]: r["outcome"] for r in app.state.mcp.audit.rows()}
    assert outcomes == {"demo__secret": "denied", "demo__nope": "unknown"}


async def test_tool_errors_are_results_and_audit_keeps_no_raw_arguments():
    app, raw = mcp_app(["demo__*"])
    args = {"text": "my password is hunter2"}
    async with sdk_session(app, raw) as s:
        assert (await s.call_tool("demo__fail", {})).isError
        assert (await s.call_tool("demo__echo", args)).content[0].text == args["text"]
    rows = app.state.mcp.audit.rows()
    assert [r["outcome"] for r in rows] == ["ok", "tool_error"]  # newest first
    assert rows[0]["args_sha256"] == args_digest(args)[0] and rows[0]["team"] == "search"
    assert "hunter2" not in json.dumps(rows)


async def test_no_grants_means_no_tools():
    app, raw = mcp_app([])
    async with sdk_session(app, raw) as s:
        assert (await s.list_tools()).tools == []


async def test_a_dead_tool_server_hides_its_tools_without_breaking_the_endpoint():
    app, raw = mcp_app(["*"], {"dead": {"command": [sys.executable, "-c", "raise SystemExit(1)"]}})
    async with sdk_session(app, raw) as s:
        assert (await s.list_tools()).tools == []


async def test_http_rules():
    app, raw = mcp_app(["demo__add"])
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "t", "version": "1"}}}
    async with client_for(app, raw) as c:
        r = await c.post("/mcp", json=init)
        assert r.json()["result"]["protocolVersion"] == "2025-06-18"
        assert (await c.post("/mcp", json={"jsonrpc": "2.0",
                                           "method": "notifications/initialized"})).status_code == 202
        assert (await c.get("/mcp")).status_code == 405
        assert (await c.post("/mcp", json=init,
                             headers={"Origin": "https://evil.example"})).status_code == 403
        assert (await c.post("/mcp", json=init,
                             headers={"Origin": "https://app.example"})).status_code == 200
        assert (await c.post("/mcp", json=init,
                             headers={"MCP-Protocol-Version": "1999-01-01"})).status_code == 400
        assert (await c.post("/mcp", content=b"{nope",
                             headers={"content-type": "application/json"})).status_code == 400
        c.headers.pop("Authorization")
        assert (await c.post("/mcp", json=init)).status_code == 401
    await app.state.mcp.up.close()
