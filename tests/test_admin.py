"""Read-only admin API for the UI: off by default, never returns key hashes, and joins a
request's spans to its ledger row by trace id."""
from conftest import ROUTE, client_for, make_app

from sluice.config import Settings
from sluice.providers.mock import MockProvider

BODY = {"model": "m", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 5}


def admin_app(**key_kw):
    settings = Settings(
        providers={"bad": {"type": "mock"}, "good": {"type": "mock"}},
        routes={"m": {"chain": [
            {"provider": "bad", "upstream_model": "big-1", "retries": 0},
            {"provider": "good", "upstream_model": "small-1", "retries": 0}]}},
        admin={"enabled": True, "recent_traces": 2})
    return make_app(settings, {"bad": MockProvider("bad", error_rate=1.0, error_status=503),
                               "good": MockProvider("good", text="ok then")},
                    key_kw={"team": "search", **key_kw})


async def test_admin_api_is_off_by_default():
    app, raw = make_app(Settings(providers={"mock": {"type": "mock"}}, routes={"m": ROUTE}))
    async with client_for(app, raw) as c:
        assert (await c.get("/v1/sluice/admin/overview")).status_code == 404


async def test_overview_shows_policy_and_breakers():
    app, raw = admin_app()
    async with client_for(app, raw) as c:
        await c.post("/v1/chat/completions", json=BODY)
        o = (await c.get("/v1/sluice/admin/overview")).json()
    assert [t["provider"] for t in o["routes"]["m"]["chain"]] == ["bad", "good"]
    assert o["providers"]["bad"]["breaker"] == {
        "state": "closed", "failures": 1, "threshold": 3, "cooldown_s": 10.0}
    assert "api_key_env" not in o["providers"]["bad"]


async def test_trace_joins_spans_and_ledger_row():
    app, raw = admin_app()
    async with client_for(app, raw) as c:
        r = await c.post("/v1/chat/completions", json=BODY)
        tid = r.headers["x-sluice-trace-id"]
        roots = (await c.get("/v1/sluice/admin/traces")).json()
        t = (await c.get(f"/v1/sluice/admin/traces/{tid}")).json()
    assert roots[0]["trace_id"] == tid and roots[0]["name"] == "chat m"
    names = [s["name"] for s in t["spans"]]
    assert names[0] == "chat m" and names.count("chat big-1") == 1 and "chat small-1" in names
    bad = next(s for s in t["spans"] if s["name"] == "chat big-1")
    assert bad["status"] == "error" and bad["attributes"]["error.type"] == "503"
    root = t["spans"][0]
    assert [e["name"] for e in root["events"]] == ["sluice.fallback"]
    assert t["ledger"]["provider"] == "good" and t["ledger"]["fallbacks"] == 1


async def test_only_the_most_recent_traces_are_kept():
    app, raw = admin_app()
    async with client_for(app, raw) as c:
        ids = [(await c.post("/v1/chat/completions", json=BODY)).headers["x-sluice-trace-id"]
               for _ in range(3)]
        roots = (await c.get("/v1/sluice/admin/traces")).json()
        first = await c.get(f"/v1/sluice/admin/traces/{ids[0]}")
    assert [r["trace_id"] for r in roots] == ids[:0:-1]
    assert first.json()["spans"] == [] and first.json()["ledger"]["trace_id"] == ids[0]


async def test_keys_never_include_secrets_and_ledger_summarises():
    app, raw = admin_app(daily_budget_usd=1.0)
    async with client_for(app, raw) as c:
        await c.post("/v1/chat/completions", json=BODY)
        keys = (await c.get("/v1/sluice/admin/keys")).json()
        rows = (await c.get("/v1/sluice/admin/ledger")).json()
        summary = (await c.get("/v1/sluice/admin/ledger/summary?by=team")).json()
        bad = await c.get("/v1/sluice/admin/ledger/summary?by=key_hash")
    assert keys[0]["name"] == "test" and keys[0]["daily_budget_usd"] == 1.0
    assert not any("hash" in k for k in keys[0]) and raw not in str(keys)
    assert rows[0]["team"] == "search" and rows[0]["fallbacks"] == 1
    assert summary[0]["group"] == "search" and summary[0]["requests"] == 1
    assert bad.status_code == 400


async def test_mcp_audit_is_listed():
    """No servers configured: the tool is "unknown", and still audited."""
    app, raw = admin_app()
    async with client_for(app, raw) as c:
        await c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                   "params": {"name": "demo__add", "arguments": {}}})
        m = (await c.get("/v1/sluice/admin/mcp")).json()
    assert m["audit"][0]["tool"] == "demo__add" and m["audit"][0]["outcome"] == "unknown"


def test_lab_policy_loads_with_admin_on():
    from pathlib import Path

    from sluice.config import load_settings

    s = load_settings(Path(__file__).parent.parent / "policies" / "lab.yaml")
    assert s.admin.enabled and "lab-failover" in s.routes
    assert s.providers["primary-down"].error_rate == 1.0
