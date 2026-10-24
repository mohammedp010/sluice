"""Phase 8: opt-in stream resume after a mid-stream failure."""
import json

import pytest
from conftest import client_for, make_app

from sluice.config import Settings
from sluice.providers.base import ChatParams, Chunk, Message
from sluice.providers.mock import MockProvider
from sluice.router.resume import CONTINUE, continuation_params, overlap

URL = "/v1/chat/completions"
FULL = "alpha beta gamma delta epsilon zeta"


@pytest.mark.parametrize("prefix,head,expected", [
    ("1\n2\n3\n", "4\n5\n", 0),                      # clean continuation
    ("alpha beta gamma ", FULL, len("alpha beta gamma ")),  # started over: drop the repeat
    ("the cat sat on the mat. ", "on the mat. Then", len("on the mat. ")),  # restated tail
    ("ends with x ", "x y z", 0),                    # 1-2 char "overlaps" are coincidence
])
def test_overlap(prefix, head, expected):
    assert overlap(prefix, head) == expected


def test_continuation_params():
    p = ChatParams("m", [Message("user", "count")], max_tokens=100)
    pre = continuation_params(p, "1 2 3 " * 20, "prefill")
    assert [m.role for m in pre.messages] == ["user", "assistant"]
    assert pre.max_tokens == 100 - 120 // 4  # the cap covers both segments
    ins = continuation_params(p, "1 2", "instruct")
    assert [m.role for m in ins.messages] == ["user", "assistant", "user"]
    assert ins.messages[-1].content == CONTINUE


def resume_app(strategy="prefill", backup=True, primary_cut=3):
    providers = {"primary": {"type": "mock"}}
    chain = [{"provider": "primary", "upstream_model": "a", "retries": 0,
              "price_in_per_mtok": 1e6, "price_out_per_mtok": 1e6}]
    mocks = {"primary": MockProvider("primary", text=FULL, cut_stream_after=primary_cut)}
    if backup:
        providers["backup"] = {"type": "mock"}
        chain.append({"provider": "backup", "upstream_model": "b", "retries": 0,
                      "price_in_per_mtok": 1e6, "price_out_per_mtok": 1e6})
        # a backup that ignores the prefix and starts over: the worst case for the seam
        mocks["backup"] = MockProvider("backup", text=FULL)
    settings = Settings(providers=providers,
                        routes={"m": {"chain": chain, "stream_resume": strategy}})
    return make_app(settings, mocks)


def stream_text(r) -> tuple[str, list[dict]]:
    events = [json.loads(e[6:]) for e in r.text.split("\n\n") if e.startswith("data: {")]
    text = "".join(e["choices"][0]["delta"].get("content", "")
                   for e in events if e.get("choices"))
    return text, events


BODY = {"model": "m", "messages": [{"role": "user", "content": "go"}], "stream": True}


async def test_resumed_stream_is_seamless_even_when_the_backup_starts_over():
    app, raw = resume_app()
    async with client_for(app, raw) as c:
        r = await c.post(URL, json=BODY)
    text, events = stream_text(r)
    assert text == FULL  # the repeated "alpha beta gamma " was cut at the seam
    assert "[DONE]" in r.text and not any("error" in e for e in events)
    root = next(s for s in app.state.spans.get_finished_spans() if s.name == "chat m")
    assert root.attributes["sluice.outcome"] == "resumed"
    assert root.attributes["sluice.resume.trimmed_chars"] == len("alpha beta gamma ")
    ev = next(e for e in root.events if e.name == "sluice.stream_resume")
    assert ev.attributes["sluice.next_provider"] == "backup"
    row = app.state.ledger.last()
    assert row["provider"] == "backup" and row["outcome"] == "resumed"
    # both segments billed: primary estimated (cut, no usage), backup as reported
    assert row["cost_micro_usd"] > 0 and row["completion_tokens"] >= 6


async def test_off_keeps_adr_0001_behaviour():
    app, raw = resume_app(strategy="off")
    async with client_for(app, raw) as c:
        r = await c.post(URL, json=BODY)
    text, events = stream_text(r)
    assert text == "alpha beta gamma " and events[-1]["error"]["code"] == "stream_interrupted"
    assert "[DONE]" not in r.text


async def test_nowhere_to_resume_ends_with_the_original_error_and_one_breaker_strike():
    app, raw = resume_app(backup=False)
    async with client_for(app, raw) as c:
        r = await c.post(URL, json=BODY)
    _, events = stream_text(r)
    assert events[-1]["error"]["code"] == "stream_interrupted"
    assert app.state.executor.breakers["primary"]._failures == 1  # reported once, not twice


async def test_client_disconnect_mid_first_segment_closes_the_upstream():
    from sluice.router.resume import ResumableStream

    closed = {}

    async def upstream():
        try:
            for i in range(100):
                yield Chunk(content=f"t{i} ")
        finally:
            closed["yes"] = True

    app, _ = resume_app()
    rs = ResumableStream(app.state.executor, app.state.settings.routes["m"],
                         ChatParams("", [Message("user", "go")]), "prefill")
    from sluice.router.executor import Trail
    gen = rs.chunks(None, upstream(), Trail(provider="primary"))
    await anext(gen)
    await gen.aclose()  # the client went away
    assert closed == {"yes": True}


async def test_resume_attempts_and_fallbacks_belong_to_the_request_trace():
    """The continuation starts while the body streams, after the handler has left the request
    span's context. Its attempt spans and fallback events must still land on that request."""
    chain = [{"provider": p, "upstream_model": p, "retries": 0} for p in ("primary", "down", "backup")]
    settings = Settings(providers={p: {"type": "mock"} for p in ("primary", "down", "backup")},
                        routes={"m": {"chain": chain, "stream_resume": "prefill"}})
    app, raw = make_app(settings, {
        "primary": MockProvider("primary", text=FULL, cut_stream_after=3),
        "down": MockProvider("down", error_rate=1.0, error_status=503),
        "backup": MockProvider("backup", text=FULL)})
    async with client_for(app, raw) as c:
        r = await c.post(URL, json=BODY)
    assert stream_text(r)[0] == FULL
    spans = app.state.spans.get_finished_spans()
    root = next(s for s in spans if s.name == "chat m")
    assert {s.context.trace_id for s in spans} == {root.context.trace_id}  # no orphan traces
    attempts = {s.name: s for s in spans if s.name in ("chat down", "chat backup")}
    assert set(attempts) == {"chat down", "chat backup"}
    assert all(s.parent.span_id == root.context.span_id for s in attempts.values())
    events = [(e.name, dict(e.attributes)) for e in root.events]
    assert ("sluice.fallback", {"sluice.provider": "down", "error.type": "503",
                                "sluice.next_provider": "backup"}) in events
    assert [n for n, _ in events].count("sluice.stream_resume") == 1
