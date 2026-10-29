"""Behaviour watch (ADR 0012): each rule fires on its signal and takes its action, actions are
reversible, `enforce: false` turns them into alerts, and lockdown is one command."""
import asyncio
import json

import httpx
import pytest
from conftest import ROUTE, make_app
from mcp.shared.exceptions import McpError
from test_mcp import SERVER, sdk_session

from sluice import admin
from sluice.auth.keys import KeyStore
from sluice.config import Settings
from sluice.limits.state import RedisState
from sluice.providers.mock import MockProvider

BODY = {"model": "m", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 5}
PRICED = {"chain": [{"provider": "mock", "upstream_model": "x", "retries": 0,
                     "price_in_per_mtok": 1e5, "price_out_per_mtok": 1e5}]}  # $0.40 a request


def app_with(watch=None, routes=None, mcp=None, **key_kw):
    settings = Settings(providers={"mock": {"type": "mock"}}, routes=routes or {"m": ROUTE},
                        mcp=mcp or {}, watch=watch or {}, admin={"enabled": True})
    return make_app(settings, key_kw=key_kw or None)


async def req(app, raw, peer="10.0.0.1", method="chat", **kw):
    t = httpx.ASGITransport(app=app, client=(peer, 40000))
    async with httpx.AsyncClient(transport=t, base_url="http://test",
                                 headers={"Authorization": f"Bearer {raw}"}) as c:
        if method == "chat":
            return await c.post("/v1/chat/completions", json=BODY)
        if method == "tool":
            return await c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                              "params": {"name": kw["tool"], "arguments": {}}})
        return await c.get(method)


def alerts(app, rule=None):
    return [a for a in app.state.watch.alerts.recent if rule is None or a["rule"] == rule]


async def test_r1_probing_for_tools_suspends_the_key():
    app, raw = app_with(watch={"probe_calls": 5})
    for i in range(5):
        r = await req(app, raw, method="tool", tool=f"files__guess_{i}")
        assert r.json()["error"]["message"].startswith("Unknown tool")
    k = app.state.keys.get(1)
    assert k.status == "suspended" and k.suspended_reason.startswith("auto:R1_probe")
    assert (await req(app, raw)).status_code == 401
    assert [a["action"] for a in alerts(app, "R1_probe")] == ["suspend"]
    app.state.keys.unsuspend(1)  # reversible
    assert (await req(app, raw)).status_code == 200


async def test_r2_burn_rate_uses_the_budget_share_or_the_absolute_fallback():
    app, raw = app_with(routes={"m": PRICED}, daily_budget_usd=2.0)  # suspend at $1.00
    codes = [(await req(app, raw)).status_code for _ in range(4)]
    assert codes == [200, 200, 200, 401]  # the 3rd brings it to $1.20 and trips R2
    assert app.state.keys.get(1).suspended_reason.startswith("auto:R2_burn_rate")

    app, raw = app_with(routes={"m": PRICED}, watch={"burn_usd": 0.6})  # no budget: absolute
    assert [(await req(app, raw)).status_code for _ in range(3)] == [200, 200, 401]
    app, raw = app_with(routes={"m": PRICED}, watch={"burn_usd": None})  # not watched
    assert [(await req(app, raw)).status_code for _ in range(3)] == [200, 200, 200]


async def test_r3_new_network_alerts_once_per_network():
    app, raw = app_with()
    for peer in ("10.0.0.1", "10.0.0.2", "10.0.0.1", "192.0.2.9", "2001:db8::1", "2001:db8::2"):
        assert (await req(app, raw, peer)).status_code == 200
    got = [(a["network"], a["first_use"]) for a in alerts(app, "R3_new_network")][::-1]
    assert got == [("10.0.0.0/24", True), ("192.0.2.0/24", False), ("2001:db8::/64", False)]
    assert app.state.keys.get(1).status == "active"  # alert only


async def test_r4_a_probe_shaped_bearer_blocks_the_ip_at_once():
    app, raw = app_with()
    assert (await req(app, raw, "203.0.113.5")).status_code == 200
    assert (await req(app, "x' UNION SELECT key FROM keys--", "203.0.113.5")).status_code == 401
    # the real key, from the same address, is refused too until the block expires
    assert (await req(app, raw, "203.0.113.5")).status_code == 401
    assert (await req(app, raw, "198.51.100.1")).status_code == 200
    (a,) = alerts(app, "R4_scanner")
    assert a["action"] == "block_ip" and a["ip"] == "203.0.113.5" and "UNION" not in json.dumps(a)


async def test_r4_many_failed_checks_block_the_ip_but_missing_keys_dont_count():
    app, raw = app_with(watch={"scanner_failures": 3})
    for _ in range(5):
        await req(app, "", "198.51.100.7", method="/v1/models")  # no key at all: not counted
    assert (await req(app, raw, "198.51.100.7")).status_code == 200
    for _ in range(3):
        await req(app, "sk-sluice-wrong", "198.51.100.7")
    assert (await req(app, raw, "198.51.100.7")).status_code == 401


async def test_r5_blocked_egress_suspends_the_key():
    app, raw = app_with(mcp={"servers": {"demo": {"command": SERVER}}},
                        allowed_tools=["demo__*"])
    app.state.settings.guard.tool_args = {"*": "block"}
    app.state.mcp.guard.tool_args = {"*": "block"}
    async with sdk_session(app, raw) as s:
        with pytest.raises(McpError):
            await s.call_tool("demo__echo", {"text": "AKIA" + "ABCDEFGHIJKLMNOP"})
    k = app.state.keys.get(1)
    assert k.status == "suspended" and "aws_access_key" in k.suspended_reason
    assert "ABCDEFGHIJKLMNOP" not in json.dumps(alerts(app))


async def test_enforce_off_only_alerts():
    app, raw = app_with(watch={"enforce": False, "probe_calls": 2})
    for i in range(4):
        await req(app, raw, method="tool", tool=f"x__{i}")
    await req(app, "bad key with spaces", "203.0.113.5")
    assert app.state.keys.get(1).status == "active"
    assert (await req(app, raw, "203.0.113.5")).status_code == 200
    assert {a["action"] for a in alerts(app) if a["rule"] != "R3_new_network"} == {"alert"}
    assert len(alerts(app, "R1_probe")) == 1  # once an hour, not once per call


async def test_tool_calls_are_rate_limited_per_key():
    app, raw = app_with(mcp={"servers": {"demo": {"command": SERVER}}},
                        allowed_tools=["demo__*"], tool_rpm=2)
    async with sdk_session(app, raw) as s:
        for _ in range(2):
            await s.call_tool("demo__add", {"a": 1, "b": 1})
        with pytest.raises(McpError) as e:
            await s.call_tool("demo__add", {"a": 1, "b": 1})
    assert "rate limited" in str(e.value)
    assert app.state.mcp.audit.rows()[0]["outcome"] == "rate_limited"


async def test_admin_api_lists_alerts():
    app, raw = app_with()
    await req(app, raw)
    r = await req(app, "", "127.0.0.1", method="/v1/sluice/admin/security")
    body = r.json()
    assert body["alerts"][0]["rule"] == "R3_new_network" and body["paid_off"] is False


async def test_webhook_gets_a_slack_compatible_post(monkeypatch):
    posted = []

    def handler(request):
        posted.append(json.loads(request.content))
        return httpx.Response(200)

    monkeypatch.setenv("SLUICE_ALERT_WEBHOOK", "https://hooks.example/x")
    app, raw = app_with()
    app.state.watch.alerts.transport = httpx.MockTransport(handler)
    await req(app, raw)
    await asyncio.sleep(0.05)
    assert posted and posted[0]["text"].startswith("Sluice R3_new_network") and \
        posted[0]["sluice"]["network"] == "10.0.0.0/24"


async def test_redis_down_fails_closed_on_every_keyed_route():
    app, raw = make_app(state=RedisState("redis://localhost:1/0"))
    for method in ("chat", "/v1/models", "tool"):
        r = await req(app, raw, method=method, tool="x__y")
        assert r.status_code == 503, method


async def test_lockdown_suspends_everything_and_turns_paid_off(tmp_path, monkeypatch, capsys):
    db = str(tmp_path / "k.db")
    s = KeyStore(db)
    raw_a, _ = s.create("a", team="red")
    s.create("b", team="blue")
    _, c = s.create("c", team="blue")
    s.suspend(c.id, "manual")
    admin.main(["--db", db, "lockdown", "--team", "red"])
    assert [k.status for k, _ in s.list()] == ["suspended", "active", "suspended"]
    assert s.flag("paid_off") is None
    admin.main(["--db", db, "lockdown"])
    assert all(k.status == "suspended" for k, _ in s.list()) and s.flag("paid_off")
    out = capsys.readouterr().out
    assert "1 key(s) suspended" in out and "paid providers off" in out

    # a gateway on this database won't call a paid provider, even with SLUICE_ALLOW_PAID=1
    monkeypatch.setenv("SLUICE_ALLOW_PAID", "1")
    s.unsuspend(1)
    settings = Settings(
        providers={"paid": {"type": "mock", "paid": True}, "free": {"type": "mock"}},
        routes={"m": {"chain": [{"provider": "paid", "upstream_model": "x", "retries": 0},
                                {"provider": "free", "upstream_model": "x", "retries": 0}]}})
    from sluice.api.app import create_app
    from sluice.limits.state import MemoryState
    from sluice.mcp.audit import McpAudit
    from sluice.mcp.pins import ToolPins
    from sluice.telemetry.ledger import UsageLedger
    gw = create_app(settings, {"paid": MockProvider("paid", text="PAID"),
                               "free": MockProvider("free", text="free")},
                    state=MemoryState(), keystore=s, ledger=UsageLedger(":memory:"),
                    mcp_audit=McpAudit(":memory:"), tool_pins=ToolPins(":memory:"))
    r = await req(gw, raw_a)
    assert r.status_code == 200 and r.headers["x-sluice-provider"] == "free"

    admin.main(["--db", db, "lockdown", "--lift"])
    assert [k.status for k, _ in s.list()] == ["active", "active", "suspended"]  # c: manual
    assert s.flag("paid_off") is None


async def test_security_report_reads_what_containment_left_on_disk(tmp_path, capsys):
    from sluice import report
    from sluice.mcp.audit import McpAudit
    from sluice.mcp.pins import ToolPins

    db = str(tmp_path / "s.db")
    keys = KeyStore(db)
    _, k = keys.create("agent", team="red")
    keys.suspend(k.id, "auto:R1_probe: 10 refused tool calls")
    keys.set_flag("paid_off", "lockdown")
    audit = McpAudit(db)
    await audit.record(key_name="agent", tool="mail__send", outcome="egress_blocked",
                       egress=json.dumps([{"where": "tool_args", "type": "aws_access_key",
                                           "path": "body", "start": 6, "len": 20}]))
    await audit.record(key_name="agent", tool="files__read", outcome="unknown")
    p = ToolPins(db)
    p.observe("s__t", {"name": "t", "description": "a"})
    p.observe("s__t", {"name": "t", "description": "b"})
    report.main(["--db", db, "--security"])
    out = capsys.readouterr().out
    for want in ("auto:R1_probe", "LOCKDOWN", "egress_blocked", "aws_access_key", "unknown",
                 "s__t", "quarantined"):
        assert want in out, want
