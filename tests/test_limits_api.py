import asyncio
import os

import pytest
import redis
from conftest import client_for, make_app

from sluice.config import Settings
from sluice.limits.state import RedisState
from sluice.providers.mock import MockProvider

BODY = {"model": "m", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 3}
URL = "/v1/chat/completions"
REDIS_URL = os.environ.get("SLUICE_TEST_REDIS", "redis://localhost:6399/15")


async def test_missing_or_bad_key_401(client_factory):
    c = client_factory()
    c.headers.pop("Authorization")
    r = await c.post(URL, json=BODY)
    assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"
    c.headers["Authorization"] = "Bearer sk-sluice-nope"
    assert (await c.post(URL, json=BODY)).status_code == 401


async def test_model_not_allowed_403(client_factory):
    c = client_factory(key_kw={"allowed_models": ["other"]})
    r = await c.post(URL, json=BODY)
    assert r.status_code == 403 and r.json()["error"]["code"] == "model_not_allowed"


async def test_rpm_limit_429_with_retry_after(client_factory):
    c = client_factory(key_kw={"rpm": 2})
    codes = [(await c.post(URL, json=BODY)).status_code for _ in range(3)]
    assert codes == [200, 200, 429]
    r = await c.post(URL, json=BODY)
    assert int(r.headers["retry-after"]) >= 1 and r.json()["error"]["code"] == "rate_limit_exceeded"


async def test_tpm_limit_and_oversize_request(client_factory):
    c = client_factory(key_kw={"tpm": 100})
    big = {**BODY, "max_tokens": 5000}
    assert (await c.post(URL, json=big)).status_code == 429          # can never fit
    small = {**BODY, "max_tokens": 40}                               # est ~ 45 tokens
    # reconciliation refunds unused estimate, so this many small calls still fit:
    codes = [(await c.post(URL, json=small)).status_code for _ in range(6)]
    assert codes.count(200) >= 5


async def test_reconciliation_refunds_unused_estimate(client_factory):
    # est ~ 3 prompt + max_tokens 100; actual completion is 3 words. Without refund the 2nd
    # call would be rejected (est 107 > 150 - 107).
    c = client_factory(key_kw={"tpm": 150})
    body = {**BODY, "max_tokens": 100}
    assert [(await c.post(URL, json=body)).status_code for _ in range(3)] == [200, 200, 200]


async def test_keys_do_not_leak_into_each_other():
    app, raw_a = make_app(key_kw={"rpm": 1})
    raw_b, _ = app.state.keys.create("b", rpm=1)
    async with client_for(app, raw_a) as a, client_for(app, raw_b) as b:
        assert (await a.post(URL, json=BODY)).status_code == 200
        assert (await a.post(URL, json=BODY)).status_code == 429
        assert (await b.post(URL, json=BODY)).status_code == 200


def priced_settings(**kw):
    route = {"chain": [{"provider": "mock", "upstream_model": "x", "retries": 0,
                        "price_in_per_mtok": 1_000_000, "price_out_per_mtok": 1_000_000}]}
    # Fake prices drain a budget in seconds, which the watch's burn-rate rule (R2) would
    # rightly suspend. These tests are about budgets, so it is off here (tests/test_watch.py).
    return Settings(providers={"mock": {"type": "mock"}}, routes={"m": route},
                    watch={"enabled": False}, **kw)


async def test_budget_exhaustion_blocks_further_calls():
    # price $1/token: a call using 4 tokens ($4) blows a $5 daily budget after two calls
    app, raw = make_app(priced_settings(), key_kw={"daily_budget_usd": 5.0})
    async with client_for(app, raw) as c:
        codes = [(await c.post(URL, json=BODY)).status_code for _ in range(3)]
        assert codes == [200, 200, 429]
        err = (await c.post(URL, json=BODY)).json()["error"]
        assert err["code"] == "budget_exceeded"


async def test_streaming_requests_are_billed_too():
    app, raw = make_app(priced_settings(), key_kw={"daily_budget_usd": 5.0})
    async with client_for(app, raw) as c:
        for _ in range(2):
            r = await c.post(URL, json={**BODY, "stream": True})
            assert "[DONE]" in r.text
        assert (await c.post(URL, json=BODY)).status_code == 429


async def test_failed_request_refunds_tokens(client_factory):
    c = client_factory(key_kw={"tpm": 60, "rpm": 100}, error_rate=1.0)
    body = {**BODY, "max_tokens": 40}
    codes = [(await c.post(URL, json=body)).status_code for _ in range(4)]
    assert set(codes) <= {502, 504}  # (504 once the breaker opens) --  would hit 429 if failures kept their estimate


async def test_body_size_cap(client_factory):
    c = client_factory()
    huge = {"model": "m", "messages": [{"role": "user", "content": "x" * 1_100_000}]}
    assert (await c.post(URL, json=huge)).status_code == 413


async def test_paid_provider_blocked_without_opt_in(monkeypatch):
    monkeypatch.delenv("SLUICE_ALLOW_PAID", raising=False)
    settings = Settings(
        providers={"paid": {"type": "mock", "paid": True}, "free": {"type": "mock"}},
        routes={"m": {"chain": [{"provider": "paid", "upstream_model": "x", "retries": 0},
                                {"provider": "free", "upstream_model": "x", "retries": 0}]}})
    app, raw = make_app(settings, {"paid": MockProvider("paid", text="PAID"),
                                   "free": MockProvider("free", text="free")})
    async with client_for(app, raw) as c:
        r = await c.post(URL, json=BODY)
        assert r.headers["x-sluice-provider"] == "free"


async def test_paid_provider_daily_cap(monkeypatch):
    monkeypatch.setenv("SLUICE_ALLOW_PAID", "1")
    settings = Settings(
        limits={"paid_daily_call_cap": 2},
        providers={"paid": {"type": "mock", "paid": True}, "free": {"type": "mock"}},
        routes={"m": {"chain": [{"provider": "paid", "upstream_model": "x", "retries": 0},
                                {"provider": "free", "upstream_model": "x", "retries": 0}]}})
    app, raw = make_app(settings, {"paid": MockProvider("paid"), "free": MockProvider("free")},
                        key_kw={"rpm": 100})
    async with client_for(app, raw) as c:
        who = [(await c.post(URL, json=BODY)).headers["x-sluice-provider"] for _ in range(4)]
    assert who == ["paid", "paid", "free", "free"]


async def test_limit_holds_across_two_gateway_instances():
    try:
        redis.Redis.from_url(REDIS_URL).flushdb()
    except redis.RedisError:
        pytest.skip("no redis")
    s1, s2 = RedisState(REDIS_URL), RedisState(REDIS_URL)
    app1, raw = make_app(key_kw={"rpm": 10}, state=s1)
    app2, _ = make_app(state=s2)
    app2.state.keys = app1.state.keys  # same key store, as two replicas sharing one DB would
    async with client_for(app1, raw) as c1, client_for(app2, raw) as c2:
        results = await asyncio.gather(
            *[(c1 if i % 2 else c2).post(URL, json=BODY) for i in range(30)])
    codes = [r.status_code for r in results]
    assert codes.count(200) == 10 and codes.count(429) == 20
    await s1.aclose(); await s2.aclose()


async def test_redis_down_fails_closed_with_503():
    app, raw = make_app(state=RedisState("redis://localhost:1/0"))
    async with client_for(app, raw) as c:
        r = await c.post(URL, json=BODY)
    assert r.status_code == 503 and r.json()["error"]["code"] == "limiter_unavailable"
