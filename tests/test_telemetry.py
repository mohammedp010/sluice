"""Phase 4: a request, including a failed-over one, can be explained from its trace, and its
cost is attributed to the key's team/app."""
import json

import httpx
from conftest import client_for, make_app
from opentelemetry.trace import SpanKind, StatusCode

from sluice.config import Settings
from sluice.providers.mock import MockProvider
from sluice.report import render

URL = "/v1/chat/completions"
BODY = {"model": "m", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 5}


def failover_app(**key_kw):
    settings = Settings(
        providers={"bad": {"type": "mock"}, "good": {"type": "mock"}},
        routes={"m": {"chain": [
            {"provider": "bad", "upstream_model": "big-1", "retries": 1},
            {"provider": "good", "upstream_model": "small-1", "retries": 0,
             "price_in_per_mtok": 1_000_000, "price_out_per_mtok": 2_000_000}]}})
    return make_app(settings, {"bad": MockProvider("bad", error_rate=1.0, error_status=503),
                               "good": MockProvider("good", text="ok then")},
                    key_kw={"team": "search", "app": "web", **key_kw})


def by_name(spans):
    out = {}
    for s in spans:
        out.setdefault(s.name, []).append(s)
    return out


def metric_points(app) -> dict[str, list]:
    data = app.state.metrics_reader.get_metrics_data()
    return {m.name: list(m.data.data_points)
            for rm in data.resource_metrics for sm in rm.scope_metrics for m in sm.metrics}


async def test_failed_over_request_is_explained_by_its_trace():
    app, raw = failover_app()
    async with client_for(app, raw) as c:
        r = await c.post(URL, json=BODY)
    assert r.status_code == 200
    spans = by_name(app.state.spans.get_finished_spans())
    root = spans["chat m"][0]
    assert root.kind is SpanKind.SERVER
    assert r.headers["x-sluice-trace-id"] == format(root.context.trace_id, "032x")

    # children: auth, limits, cache stub, then 2 failed attempts on bad + 1 ok on good
    kids = [s for s in app.state.spans.get_finished_spans() if s.parent
            and s.parent.span_id == root.context.span_id]
    assert [s.name for s in kids] == ["sluice.auth", "sluice.limits", "sluice.cache",
                                      "chat big-1", "chat big-1", "chat small-1"]
    bad, good = spans["chat big-1"], spans["chat small-1"][0]
    assert all(s.kind is SpanKind.CLIENT and s.status.status_code is StatusCode.ERROR
               and s.attributes["error.type"] == "503" for s in bad)
    assert good.attributes["gen_ai.provider.name"] == "good"
    assert good.status.status_code is StatusCode.UNSET

    # why it moved on, recorded on the request span
    (ev,) = [e for e in root.events if e.name == "sluice.fallback"]
    assert dict(ev.attributes) == {"sluice.provider": "bad", "error.type": "503",
                                   "sluice.next_provider": "good"}

    # what it cost, and who pays
    a = root.attributes
    assert (a["sluice.provider"], a["gen_ai.response.model"]) == ("good", "small-1")
    assert (a["gen_ai.usage.input_tokens"], a["gen_ai.usage.output_tokens"]) == (1, 2)
    assert a["sluice.cost_usd"] == 1 * 1.0 + 2 * 2.0
    assert (a["sluice.team"], a["sluice.app"], a["sluice.fallbacks"]) == ("search", "web", 1)

    row = app.state.ledger.by_trace(r.headers["x-sluice-trace-id"])
    assert (row["team"], row["app"], row["provider"], row["fallbacks"]) == ("search", "web", "good", 1)
    assert row["cost_micro_usd"] == 5_000_000 and row["attempts"] == 3


async def test_incoming_traceparent_is_continued():
    app, raw = make_app()
    tid = "4bf92f3577b34da6a3ce929d0e0e4736"
    async with client_for(app, raw) as c:
        r = await c.post(URL, json=BODY,
                         headers={"traceparent": f"00-{tid}-00f067aa0ba902b7-01"})
    assert r.headers["x-sluice-trace-id"] == tid
    root = by_name(app.state.spans.get_finished_spans())["chat m"][0]
    assert root.parent.span_id == 0x00F067AA0BA902B7


async def test_stream_span_ends_with_the_stream():
    app, raw = make_app()
    async with client_for(app, raw) as c:
        r = await c.post(URL, json={**BODY, "stream": True})
    assert "[DONE]" in r.text
    root = by_name(app.state.spans.get_finished_spans())["chat m"][0]
    assert root.attributes["sluice.outcome"] == "ok"
    assert root.attributes["gen_ai.usage.output_tokens"] == 3


async def test_mid_stream_cut_marks_request_span_as_error():
    app, raw = make_app(cut_stream_after=1)
    async with client_for(app, raw) as c:
        await c.post(URL, json={**BODY, "stream": True})
    root = by_name(app.state.spans.get_finished_spans())["chat m"][0]
    assert root.attributes["sluice.outcome"] == "stream_interrupted"
    assert root.status.status_code is StatusCode.ERROR
    assert app.state.ledger.last()["outcome"] == "stream_interrupted"


async def test_rejected_requests_are_traced_but_cost_nothing():
    app, raw = make_app(key_kw={"rpm": 1})
    async with client_for(app, raw) as c:
        await c.post(URL, json=BODY)
        r = await c.post(URL, json=BODY)
    assert r.status_code == 429 and "x-sluice-trace-id" in r.headers
    roots = by_name(app.state.spans.get_finished_spans())["chat m"]
    assert [s.attributes["sluice.outcome"] for s in roots] == ["ok", "rate_limit_exceeded"]
    assert len(app.state.ledger.summary("key")) == 1 and app.state.ledger.summary("key")[0][1] == 1


async def test_metrics_cover_requests_attempts_fallbacks_tokens_breakers():
    app, raw = failover_app()
    async with client_for(app, raw) as c:
        await c.post(URL, json=BODY)
    pts = metric_points(app)
    assert sum(p.value for p in pts["sluice.requests"]) == 1
    errs = [p for p in pts["gen_ai.client.operation.duration"]
            if p.attributes.get("error.type") == "503"]
    assert errs and errs[0].count == 2
    assert errs[0].explicit_bounds[0] == 0.01  # GenAI buckets, not the SDK's millisecond ones
    assert pts["sluice.fallbacks"][0].attributes["error.type"] == "503"
    assert {p.attributes["gen_ai.token.type"] for p in pts["gen_ai.client.token.usage"]} == {
        "input", "output"}
    assert sum(p.value for p in pts["sluice.cost"]) == 5.0
    assert {p.attributes["sluice.provider"]: p.value for p in pts["sluice.breaker.state"]} == {
        "bad": 0, "good": 0}  # 2 failures < threshold 3


async def test_no_per_key_attributes_on_metrics():
    # one time series per key would be a cardinality bomb; per-key cost lives in the ledger
    app, raw = failover_app()
    async with client_for(app, raw) as c:
        await c.post(URL, json=BODY)
    keys = {k for pts in metric_points(app).values() for p in pts for k in p.attributes}
    assert not keys & {"sluice.key.id", "sluice.team", "sluice.app"}


async def test_cost_report_groups_by_team():
    app, raw = failover_app()
    raw2, _ = app.state.keys.create("other", team="ads")
    async with client_for(app, raw) as a, client_for(app, raw2) as b:
        for _ in range(2):
            await a.post(URL, json=BODY)
        await b.post(URL, json=BODY)
    rows = app.state.ledger.summary("team")
    assert [(r[0], r[1], r[6]) for r in rows] == [("search", 2, 10.0), ("ads", 1, 5.0)]
    text = render(rows, "team")
    assert text.splitlines()[-1].split()[:2] == ["TOTAL", "3"]


async def test_upstream_requests_carry_traceparent():
    from sluice.providers.openai_compat import OpenAICompatProvider

    seen = {}

    def handler(req):
        seen["tp"] = req.headers.get("traceparent")
        return httpx.Response(200, json={"choices": [{"message": {"content": "x"}}]})

    up = OpenAICompatProvider("up", "http://up/v1", client=httpx.AsyncClient(
        base_url="http://up/v1", transport=httpx.MockTransport(handler)))
    settings = Settings(providers={"up": {"type": "openai_compat", "base_url": "http://up/v1"}},
                        routes={"m": {"provider": "up", "upstream_model": "x", "retries": 0}})
    app, raw = make_app(settings, {"up": up})
    async with client_for(app, raw) as c:
        r = await c.post(URL, json=BODY)
    attempt = by_name(app.state.spans.get_finished_spans())["chat x"][0]
    # version-traceid-parentid-flags; the upstream's parent is the *attempt* span, not the root
    assert seen["tp"].split("-")[:3] == [
        "00", r.headers["x-sluice-trace-id"], f"{attempt.context.span_id:016x}"]
    assert json.loads(r.text)["choices"][0]["message"]["content"] == "x"
