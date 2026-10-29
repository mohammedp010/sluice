"""Containment for virtual keys (ADR 0011): expiry, network binding, suspend and revoke,
rotation with a grace period, and revoking many at once. Every refusal is the same 401 to the
caller; the reason goes to telemetry only."""
import httpx
import pytest
from conftest import ROUTE, make_app

from sluice import admin
from sluice.auth.keys import KeyStore, _epoch, client_ip, parse_duration, parse_networks
from sluice.config import Settings

BODY = {"model": "m", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 3}
T0 = 1_800_000_000.0  # a fixed "now"
DAY = 86400


def test_durations():
    assert [parse_duration(s) for s in ("45s", "30m", "24h", "90d")] == [45, 1800, 86400, 7776000]
    for bad in ("", "30", "1w", "-1d", "1.5h"):
        with pytest.raises(ValueError):
            parse_duration(bad)


def test_key_expires():
    s = KeyStore(":memory:")
    raw, k = s.create("t", ttl_s=DAY, now=T0)
    assert _epoch(k.expires_at) == T0 + DAY
    assert s.check(raw, now=T0 + DAY - 1)[1] == "ok"
    assert s.check(raw, now=T0 + DAY) == (None, "expired")
    forever, _ = s.create("f", now=T0)
    assert s.check(forever, now=T0 + 100 * 365 * DAY)[1] == "ok"


def test_network_binding():
    s = KeyStore(":memory:")
    raw, _ = s.create("t", allowed_cidrs=["10.1.0.0/16", "2001:db8::/32"])
    assert s.check(raw, "10.1.2.3")[1] == "ok" and s.check(raw, "2001:db8::7")[1] == "ok"
    for ip in ("10.2.0.1", "203.0.113.9", None, "not-an-ip"):
        assert s.check(raw, ip) == (None, "network")
    with pytest.raises(ValueError):
        s.create("typo", allowed_cidrs=["10.1.0/16"])


def test_suspend_is_reversible_and_revoke_is_final():
    s = KeyStore(":memory:")
    raw, k = s.create("t")
    assert s.suspend(k.id, "probe") and s.check(raw) == (None, "suspended")
    assert s.get(k.id).suspended_reason == "probe"
    assert s.unsuspend(k.id) and s.check(raw)[1] == "ok"
    assert s.revoke(k.id) and s.check(raw) == (None, "revoked")
    assert not s.suspend(k.id, "x") and not s.unsuspend(k.id)
    assert s.check("sk-sluice-nope") == (None, "unknown")


def test_rotation_overlaps_for_the_grace_then_only_the_new_key_works():
    s = KeyStore(":memory:")
    old_raw, old = s.create("svc", team="t", allowed_tools=["f__*"], rpm=7, ttl_s=30 * DAY,
                            allowed_cidrs=["10.0.0.0/8"], now=T0)
    new_raw, new = s.rotate(old.id, grace_s=3600, now=T0 + DAY)
    assert (new.team, new.allowed_tools, new.rpm, new.allowed_cidrs) == (
        "t", ["f__*"], 7, ["10.0.0.0/8"])
    assert new.limit_id == old.id == old.limit_id  # one lineage: limits and spend are shared
    assert _epoch(new.expires_at) == T0 + DAY + 30 * DAY  # the old key's lifetime, from now
    ip = "10.0.0.1"
    assert s.check(old_raw, ip, now=T0 + DAY + 3599)[1] == "ok"
    assert s.check(new_raw, ip, now=T0 + DAY + 3599)[1] == "ok"
    assert s.check(old_raw, ip, now=T0 + DAY + 3600) == (None, "expired")
    # rotating again keeps the root lineage
    _, newer = s.rotate(new.id, grace_s=0, now=T0 + 2 * DAY)
    assert newer.limit_id == old.id and s.check(new_raw, ip, now=T0 + 2 * DAY)[1] == "expired"


def test_grace_never_extends_a_key_and_revoked_keys_dont_rotate():
    s = KeyStore(":memory:")
    _, k = s.create("t", ttl_s=60, now=T0)
    s.rotate(k.id, grace_s=DAY, now=T0)
    assert _epoch(s.get(k.id).expires_at) == T0 + 60
    s.revoke(k.id)
    assert s.rotate(k.id, grace_s=0) is None
    _, sus = s.create("s")
    s.suspend(sus.id, "leak")
    new_raw, _ = s.rotate(sus.id, grace_s=DAY)
    assert s.get(sus.id).status == "suspended" and s.check(new_raw)[1] == "ok"


def test_revoke_many():
    s = KeyStore(":memory:")
    a = s.create("a", team="red", now=T0)[1]
    b = s.create("b", team="red", now=T0 + 2 * DAY)[1]
    c = s.create("c", team="blue", now=T0)[1]
    with pytest.raises(ValueError):
        s.revoke_many()
    old = s.get(a.id).created_at[:10]  # T0's date
    assert s.revoke_many(team="red", created_before=f"{old} 23:59:59") == 1
    assert [s.get(i).status for i in (a.id, b.id, c.id)] == ["revoked", "active", "active"]
    assert s.revoke_many(everything=True) == 2  # the already-revoked one isn't counted


def test_pre_containment_database_is_migrated(tmp_path):
    import sqlite3

    db = tmp_path / "old.db"
    s = KeyStore(str(db))
    raw, k = s.create("old")
    s._db.close()
    con = sqlite3.connect(db)  # rebuild it as a phase 8 database: no containment columns
    con.executescript("""
        CREATE TABLE k2 AS SELECT id, key_hash, key_hint, name, team, app, allowed_models, rpm,
            tpm, daily_budget_usd, monthly_budget_usd, revoked, created_at, allowed_tools FROM keys;
        DROP TABLE keys; ALTER TABLE k2 RENAME TO keys;
        INSERT INTO keys (id, key_hash, key_hint, name, rpm, tpm, revoked, created_at,
            allowed_models, allowed_tools, team, app)
            VALUES (9, 'h', 'abcd', 'gone', 1, 1, 1, '2026-01-01 00:00:00', '["*"]', '[]', '', '');
    """)
    con.close()
    s = KeyStore(str(db))
    assert s.check(raw)[0].id == k.id and s.get(k.id).expires_at is None
    assert s.get(9).status == "revoked"


def test_client_ip_believes_only_trusted_proxies():
    trusted = parse_networks(["10.0.0.0/8", "127.0.0.1/32"])
    assert client_ip("203.0.113.5", "1.2.3.4", trusted) == "203.0.113.5"  # untrusted peer
    assert client_ip("10.0.0.2", "198.51.100.1", trusted) == "198.51.100.1"
    # the client wrote the leftmost entry; the first untrusted hop from the right is real
    assert client_ip("10.0.0.2", "6.6.6.6, 198.51.100.1, 10.0.0.9", trusted) == "198.51.100.1"
    assert client_ip("10.0.0.2", None, trusted) == "10.0.0.2"
    assert client_ip("10.0.0.2", "garbage", trusted) is None
    assert client_ip("127.0.0.1", "203.0.113.5", []) == "127.0.0.1"


def gateway(trusted=(), **key_kw):
    settings = Settings(providers={"mock": {"type": "mock"}}, routes={"m": ROUTE},
                        auth={"trusted_proxies": list(trusted)})
    return make_app(settings, key_kw=key_kw)


def rejections(app) -> dict[str, int]:
    out = {}
    for rm in app.state.metrics_reader.get_metrics_data().resource_metrics:
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                if m.name == "sluice.auth.rejections":
                    for p in m.data.data_points:
                        out[p.attributes["sluice.auth.result"]] = p.value
    return out


async def call(app, raw, peer="203.0.113.7", xff=None, path="/v1/chat/completions"):
    t = httpx.ASGITransport(app=app, client=(peer, 40000))
    headers = {"Authorization": f"Bearer {raw}", **({"X-Forwarded-For": xff} if xff else {})}
    async with httpx.AsyncClient(transport=t, base_url="http://test", headers=headers) as c:
        if path == "/v1/chat/completions":
            return await c.post(path, json=BODY)
        if path == "/mcp":
            return await c.post(path, json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
        return await c.get(path)


async def test_every_refusal_is_the_same_401_and_telemetry_has_the_reason():
    app, raw = gateway(allowed_cidrs=["10.0.0.0/8"])
    keys = app.state.keys
    expired, _ = keys.create("e", ttl_s=1, now=1_700_000_000)  # made in 2023
    sus_raw, sus = keys.create("s")
    keys.suspend(sus.id, "probe")
    bodies = set()
    for k, peer in ((raw, "203.0.113.7"), (expired, "10.0.0.1"), (sus_raw, "10.0.0.1"),
                    ("sk-sluice-nope", "10.0.0.1")):
        r = await call(app, k, peer)
        assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"
        bodies.add(r.text)
    assert len(bodies) == 1
    assert (await call(app, raw, "10.9.9.9")).status_code == 200
    assert rejections(app) == {"network": 1, "expired": 1, "suspended": 1, "unknown": 1}
    span = next(s for s in app.state.spans.get_finished_spans() if s.name == "sluice.auth"
                and s.attributes.get("sluice.auth.result") == "network")
    assert span is not None


@pytest.mark.parametrize("path", ["/v1/models", "/mcp"])
async def test_binding_holds_on_every_keyed_route(path):
    app, raw = gateway(allowed_cidrs=["10.0.0.0/8"])
    assert (await call(app, raw, "203.0.113.7", path=path)).status_code == 401
    assert (await call(app, raw, "10.0.0.1", path=path)).status_code == 200


async def test_forwarded_for_counts_only_from_a_trusted_proxy():
    app, raw = gateway(trusted=["172.16.0.0/12"], allowed_cidrs=["10.0.0.0/8"])
    assert (await call(app, raw, "172.17.0.1", xff="10.0.0.5")).status_code == 200
    assert (await call(app, raw, "172.17.0.1", xff="203.0.113.7")).status_code == 401
    # an untrusted caller can't claim to be inside the range
    assert (await call(app, raw, "203.0.113.7", xff="10.0.0.5")).status_code == 401
    # nor can it by prepending to a trusted proxy's header
    assert (await call(app, raw, "172.17.0.1", xff="10.0.0.5, 203.0.113.7")).status_code == 401


async def test_rotated_key_shares_the_old_keys_limits():
    app, raw = gateway(rpm=2)
    new_raw, _ = app.state.keys.rotate(1, grace_s=3600)
    assert (await call(app, raw)).status_code == 200
    assert (await call(app, new_raw)).status_code == 200
    assert (await call(app, new_raw)).status_code == 429  # one bucket for the lineage


async def test_admin_loopback_check_sees_through_a_trusted_proxy():
    settings = Settings(providers={"mock": {"type": "mock"}}, routes={"m": ROUTE},
                        admin={"enabled": True}, auth={"trusted_proxies": ["127.0.0.1/32"]})
    app, _ = make_app(settings)
    path = "/v1/sluice/admin/keys"
    assert (await call(app, "", "127.0.0.1", path=path)).status_code == 200
    assert (await call(app, "", "127.0.0.1", xff="203.0.113.7", path=path)).status_code == 403
    keys = (await call(app, "", "127.0.0.1", path=path)).json()
    assert keys[0]["status"] == "active" and "key_hash" not in keys[0]


def test_policy_validates_auth_settings():
    for bad in ({"key_ttl": "soon"}, {"trusted_proxies": ["10.0.0/8"]}):
        with pytest.raises(ValueError):
            Settings(providers={"mock": {"type": "mock"}}, auth=bad)


def test_cli_uses_the_policy_for_db_and_default_ttl(tmp_path, capsys):
    db = tmp_path / "k.db"
    policy = tmp_path / "p.yaml"
    policy.write_text(f"auth: {{db_path: {db}, key_ttl: 7d}}\n"
                      "providers: {mock: {type: mock}}\n")
    admin.main(["--policy", str(policy), "create-key", "--name", "a", "--cidrs", "10.0.0.0/8"])
    admin.main(["--policy", str(policy), "create-key", "--name", "b", "--team", "x",
                "--no-expiry"])
    s = KeyStore(str(db))
    a, b = s.get(1), s.get(2)
    assert _epoch(a.expires_at) - _epoch(a.created_at) == 7 * DAY and b.expires_at is None
    admin.main(["--policy", str(policy), "rotate-key", "1", "--grace", "1h"])
    assert s.get(3).limit_id == 1
    admin.main(["--policy", str(policy), "suspend-key", "2", "--reason", "leak"])
    admin.main(["--policy", str(policy), "list-keys"])
    out = capsys.readouterr().out
    assert "SUSPENDED (leak)" in out and "rotated-from=#1" in out and "from=10.0.0.0/8" in out
    admin.main(["--policy", str(policy), "revoke", "--team", "x"])
    admin.main(["--policy", str(policy), "revoke", "--all"])
    assert "revoked 1 key(s)\nrevoked 2 key(s)" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        admin.main(["--policy", str(policy), "revoke"])
