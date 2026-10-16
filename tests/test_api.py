import json

from openai import AsyncOpenAI

BODY = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}


def sse_events(text: str) -> list[str]:
    return [line[6:] for line in text.split("\n\n") if line.startswith("data: ")]


async def test_health_and_models(client_factory):
    c = client_factory()
    assert (await c.get("/healthz")).json() == {"status": "ok"}
    assert [m["id"] for m in (await c.get("/v1/models")).json()["data"]] == ["m"]


async def test_non_streaming(client_factory):
    r = await client_factory().post("/v1/chat/completions", json=BODY)
    d = r.json()
    assert r.status_code == 200
    assert d["object"] == "chat.completion"
    assert d["choices"][0]["message"]["content"] == "hello there world"
    assert d["usage"]["completion_tokens"] == 3


async def test_unknown_model_is_openai_shaped_404(client_factory):
    r = await client_factory().post("/v1/chat/completions", json={**BODY, "model": "nope"})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "model_not_found"


async def test_validation_error_422(client_factory):
    r = await client_factory().post("/v1/chat/completions", json={"model": "m", "messages": []})
    assert r.status_code == 422


async def test_max_tokens_truncates(client_factory):
    d = (await client_factory().post(
        "/v1/chat/completions", json={**BODY, "max_tokens": 2})).json()
    assert d["choices"][0]["message"]["content"] == "hello there "
    assert d["choices"][0]["finish_reason"] == "length"


async def test_streaming_shape(client_factory):
    r = await client_factory().post("/v1/chat/completions", json={**BODY, "stream": True})
    assert r.headers["content-type"].startswith("text/event-stream")
    ev = sse_events(r.text)
    assert ev[-1] == "[DONE]"
    chunks = [json.loads(e) for e in ev[:-1]]
    assert chunks[0]["choices"][0]["delta"]["role"] == "assistant"
    text = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks if c["choices"])
    assert text == "hello there world"
    assert chunks[-1]["usage"]["total_tokens"] == 1 + 3  # 1 prompt word + 3 completion tokens


async def test_upstream_error_before_stream_is_http_502(client_factory):
    c = client_factory(error_rate=1.0)
    assert (await c.post("/v1/chat/completions", json=BODY)).status_code == 502
    assert (await c.post("/v1/chat/completions", json={**BODY, "stream": True})).status_code == 502


async def test_mid_stream_cut_emits_error_event_and_no_done(client_factory):
    r = await client_factory(cut_stream_after=1).post(
        "/v1/chat/completions", json={**BODY, "stream": True})
    assert r.status_code == 200  # status was already sent
    ev = sse_events(r.text)
    assert "[DONE]" not in ev
    assert json.loads(ev[-1])["error"]["code"] == "stream_interrupted"


async def test_official_openai_sdk_works_unchanged(client_factory):
    from conftest import client_for, make_app
    app, raw = make_app()
    sdk = AsyncOpenAI(api_key=raw, base_url="http://test/v1", http_client=client_for(app, raw))
    r = await sdk.chat.completions.create(model="m", messages=[{"role": "user", "content": "hi"}])
    assert r.choices[0].message.content == "hello there world"
    stream = await sdk.chat.completions.create(
        model="m", messages=[{"role": "user", "content": "hi"}], stream=True)
    got = "".join([c.choices[0].delta.content or "" async for c in stream if c.choices])
    assert got == "hello there world"


async def test_failover_visible_in_headers_and_health():
    from conftest import client_for, make_app

    from sluice.config import Settings
    from sluice.providers.mock import MockProvider

    settings = Settings(
        providers={"bad": {"type": "mock"}, "good": {"type": "mock"}},
        routes={"m": {"deadline_s": 5, "chain": [
            {"provider": "bad", "upstream_model": "x", "retries": 0},
            {"provider": "good", "upstream_model": "x", "retries": 0}]}},
    )
    app, raw = make_app(settings, {"bad": MockProvider("bad", error_rate=1.0),
                                   "good": MockProvider("good", text="ok then")})
    async with client_for(app, raw) as c:
        r = await c.post("/v1/chat/completions", json=BODY)
        assert r.status_code == 200 and r.json()["choices"][0]["message"]["content"] == "ok then"
        assert r.headers["x-sluice-provider"] == "good" and r.headers["x-sluice-attempts"] == "2"
        for _ in range(3):
            await c.post("/v1/chat/completions", json=BODY)
        assert (await c.get("/v1/sluice/health")).json() == {"bad": "open", "good": "closed"}
