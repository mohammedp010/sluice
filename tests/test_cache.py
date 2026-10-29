"""Phase 5: semantic cache. A fake embedder makes similarity exact and deterministic here; the
real models are measured separately in bench/cache_eval.py."""
import json

import httpx
import numpy as np
import pytest
from conftest import client_for, make_app

from sluice.cache.embed import OpenAICompatEmbedder, normalise
from sluice.cache.semantic import CachedAnswer, SemanticCache
from sluice.cache.store import MemoryVectorStore
from sluice.config import Settings
from sluice.providers.base import Message, Usage
from sluice.providers.mock import MockProvider

URL = "/v1/chat/completions"


VECS = {
    "what is the capital of france?": [1.0, 0.0, 0.0],
    "tell me france's capital city": [0.98, 0.199, 0.0],
    "what is the capital of spain?": [0.80, 0.0, 0.6],  # cos 0.8 with the first: a near miss
}


class FakeEmbedder:
    """Known texts get hand-placed vectors (VECS); cos(capital, capital-paraphrase) = 0.98."""

    name = "fake"

    def __init__(self):
        self.calls = 0
        self.fail = False

    async def embed(self, texts):
        self.calls += 1
        if self.fail:
            raise httpx.ConnectError("embedder down")
        return normalise(np.array([VECS.get(t.lower(), [0.0, 0.0, 1.0]) for t in texts]))


def vec(*v):
    return normalise(np.array([v]))[0]


# --- store -------------------------------------------------------------------------------

def test_store_returns_best_match_within_scope_only():
    s = MemoryVectorStore(10)
    s.add("a", vec(1, 0), "x", 60)
    s.add("a", vec(0.6, 0.8), "y", 60)
    s.add("b", vec(1, 0), "other tenant", 60)
    m = s.search("a", vec(1, 0))
    assert m.value == "x" and m.similarity == pytest.approx(1.0)
    assert s.search("c", vec(1, 0)) is None


def test_store_ttl_and_ring_buffer_eviction():
    t = [0.0]
    s = MemoryVectorStore(2, clock=lambda: t[0])
    s.add("a", vec(1, 0), "old", 10)
    s.add("a", vec(0, 1), "mid", 100)
    s.add("a", vec(1, 1), "new", 100)  # capacity 2: overwrites "old"
    assert s.search("a", vec(1, 0)).value != "old"
    t[0] = 50
    assert len(s) == 2
    t[0] = 200
    assert s.search("a", vec(0, 1)) is None and len(s) == 0


def test_store_resets_when_embedding_dimension_changes():
    s = MemoryVectorStore(4)
    s.add("a", vec(1, 0), "2d", 60)
    assert s.search("a", vec(1, 0, 0)) is None  # a different model's vectors are not comparable
    s.add("a", vec(1, 0, 0), "3d", 60)
    assert s.search("a", vec(1, 0, 0)).value == "3d"


# --- policy ------------------------------------------------------------------------------

def make_cache(threshold=0.95, **kw):
    return SemanticCache(FakeEmbedder(), MemoryVectorStore(100), threshold, **kw)


def msgs(*texts, system=None):
    out = [Message("system", system)] if system else []
    return out + [Message("user", t) for t in texts]


async def probe(cache, text="What is the capital of France?", **kw):
    args = {"tenant": "t", "model": "m", "messages": msgs(text), "temperature": None,
            "max_tokens": 100, **kw}
    return await cache.probe(**args)


async def test_paraphrase_hits_and_near_miss_does_not():
    c = make_cache()
    p = await probe(c)
    assert p.result == "miss"
    c.put(p, CachedAnswer("What is the capital of France?", "Paris.", "stop", Usage(5, 2)))
    hit = await probe(c, "Tell me France's capital city")
    assert hit.result == "hit" and hit.answer.content == "Paris."
    near = await probe(c, "What is the capital of Spain?")
    assert near.result == "miss" and near.match.similarity == pytest.approx(0.8)


@pytest.mark.parametrize("kw,reason", [
    ({"temperature": 0.7}, "temperature"),
    ({"messages": [Message("user", "hi"), Message("assistant", "hello")]}, "not_user_turn"),
    ({"cache_control": "no-cache, no-store"}, "cache_control"),
])
async def test_skip_rules(kw, reason):
    p = await probe(make_cache(), **kw)
    assert (p.result, p.reason) == ("skip", reason)


async def test_no_cache_refreshes_and_no_store_reads_only():
    c = make_cache()
    p = await probe(c, cache_control="no-cache")  # don't serve from cache, but do store
    assert p.result == "miss" and c.put(p, CachedAnswer("What is the capital of France?", "Paris.", "stop", Usage()))
    assert (await probe(c, cache_control="no-cache")).result == "miss"
    p = await probe(c, cache_control="no-store")  # may serve, must not store
    assert p.result == "hit"
    p2 = await probe(c, "Tell me France's capital city", tenant="new", cache_control="no-store")
    assert p2.result == "miss" and not c.put(p2, CachedAnswer("What is the capital of France?", "Paris.", "stop", Usage()))


async def test_scope_includes_history_params_and_tenant():
    c = make_cache()
    p = await probe(c)
    c.put(p, CachedAnswer("What is the capital of France?", "Paris.", "stop", Usage()))
    assert (await probe(c, tenant="other")).result == "miss"
    assert (await probe(c, max_tokens=5)).result == "miss"
    assert (await probe(c, model="m2")).result == "miss"
    other_system = msgs("What is the capital of France?", system="Answer in French.")
    assert (await probe(c, messages=other_system)).result == "miss"


@pytest.mark.parametrize("a,b,diff", [
    ("Convert 5 km to miles.", "convert 5 KM to miles", -1),  # plain repeat
    ("Convert 5 km to miles.", "Convert 5 miles to km.", 0),  # reordered
    ("What is the capital of Austria?", "What is the capital of Australia?", 2),
    ("Is a tomato a fruit or a vegetable?", "Are tomatoes fruits or vegetables?", 9),
])
def test_token_diff(a, b, diff):
    from sluice.cache.semantic import token_diff
    assert token_diff(a, b) == diff


async def test_lexical_guard_vetoes_minimal_pairs_only():
    VECS["what is the capital of austria?"] = [0.999, 0.0447, 0.0]  # embeddings say "same"
    try:
        c = make_cache()
        c.put(await probe(c), CachedAnswer("What is the capital of France?", "Paris.", "stop",
                                           Usage()))
        c.put(await probe(c, tenant="t2"), CachedAnswer(
            "What is the capital of Australia?", "Canberra.", "stop", Usage()))
        p = await probe(c, "What is the capital of Austria?", tenant="t2")
        assert (p.result, p.reason) == ("miss", "lexical_guard") and p.match.similarity > 0.99
        assert (await probe(c, "Tell me France's capital city")).result == "hit"  # a rewrite
        c.guard_max_diff = None
        assert (await probe(c, "What is the capital of Austria?", tenant="t2")).result == "hit"
    finally:
        del VECS["what is the capital of austria?"]


async def test_embedder_down_fails_open():
    c = make_cache()
    c.embedder.fail = True
    assert (await probe(c)).reason == "embed_error"


async def test_empty_answers_are_not_stored():
    c = make_cache()
    assert not c.put(await probe(c), CachedAnswer("q", "", "stop", Usage()))


async def test_openai_compat_embedder_normalises_and_keeps_order():
    def h(req):
        body = json.loads(req.content)
        assert body["input"] == ["clustering: a", "clustering: b"]
        return httpx.Response(200, json={"data": [
            {"index": 1, "embedding": [0, 2]}, {"index": 0, "embedding": [3, 4]}]})
    e = OpenAICompatEmbedder("http://up/v1", "m", prefix="clustering: ", client=httpx.AsyncClient(
        base_url="http://up/v1", transport=httpx.MockTransport(h)))
    out = await e.embed(["a", "b"])
    assert np.allclose(out, [[0.6, 0.8], [0.0, 1.0]])  # index order restored, unit length


# --- through the gateway -----------------------------------------------------------------

class CountingMock(MockProvider):
    calls = 0

    async def complete(self, params):
        self.calls += 1
        return await super().complete(params)

    async def stream(self, params):
        self.calls += 1
        async for c in super().stream(params):
            yield c


def body(text, **kw):
    return {"model": "m", "messages": [{"role": "user", "content": text}], **kw}


async def test_gateway_hit_skips_upstream_and_costs_nothing():
    cache = make_cache()
    settings = Settings(providers={"mock": {"type": "mock"}}, routes={"m": {
        "cache": True, "chain": [{"provider": "mock", "upstream_model": "x", "retries": 0,
                                  "price_in_per_mtok": 1e6, "price_out_per_mtok": 1e6}]}},
                        watch={"enabled": False})  # fake prices would trip R2 (burn rate)
    up = CountingMock(text="Paris is the capital.")
    app, raw = make_app(settings, {"mock": up}, key_kw={"team": "search"}, cache=cache)
    async with client_for(app, raw) as c:
        r1 = await c.post(URL, json=body("What is the capital of France?"))
        r2 = await c.post(URL, json=body("Tell me France's capital city"))
        r3 = await c.post(URL, json=body("Tell me France's capital city", stream=True))
    assert (r1.headers["x-sluice-cache"], r2.headers["x-sluice-cache"]) == ("miss", "hit")
    assert r2.json()["choices"][0]["message"]["content"] == "Paris is the capital."
    assert r2.headers["x-sluice-provider"] == "cache"
    assert float(r2.headers["x-sluice-cache-similarity"]) == pytest.approx(0.98, abs=1e-3)
    assert up.calls == 1  # the hit (and the streamed hit) never reached the provider

    # streamed replay of the cached answer is a well-formed SSE stream
    assert r3.headers["x-sluice-cache"] == "hit" and r3.text.rstrip().endswith("data: [DONE]")
    events = [json.loads(e[6:]) for e in r3.text.split("\n\n") if e.startswith("data: {")]
    assert "".join(e["choices"][0]["delta"].get("content", "")
                   for e in events if e["choices"]) == "Paris is the capital."

    rows = app.state.ledger.summary("provider")
    assert {r[0]: (r[1], r[6]) for r in rows} == {"mock": (1, 10.0), "cache": (2, 0.0)}


async def test_gateway_streamed_miss_is_stored_and_tenants_are_isolated():
    cache = make_cache()
    settings = Settings(providers={"mock": {"type": "mock"}}, routes={"m": {
        "cache": True, "chain": [{"provider": "mock", "upstream_model": "x", "retries": 0}]}})
    up = CountingMock(text="Paris is the capital.")
    app, raw = make_app(settings, {"mock": up}, key_kw={"team": "search"}, cache=cache)
    raw_ads, _ = app.state.keys.create("ads-key", team="ads")
    async with client_for(app, raw) as search, client_for(app, raw_ads) as ads:
        r1 = await search.post(URL, json=body("What is the capital of France?", stream=True))
        assert "[DONE]" in r1.text and r1.headers["x-sluice-cache"] == "miss"
        assert (await search.post(URL, json=body("What is the capital of France?"))
                ).headers["x-sluice-cache"] == "hit"
        assert (await ads.post(URL, json=body("What is the capital of France?"))
                ).headers["x-sluice-cache"] == "miss"  # same words, other tenant
    assert up.calls == 2


async def test_gateway_routes_without_opt_in_skip_the_cache():
    app, raw = make_app(cache=make_cache())  # default route has cache: false
    async with client_for(app, raw) as c:
        r = await c.post(URL, json=body("What is the capital of France?", model="m"))
    assert r.headers["x-sluice-cache"] == "skip"
