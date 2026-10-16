"""Phase 6: cheap-first routing with escalation on visibly bad answers."""
import json

import pytest
from conftest import client_for, make_app

from sluice.config import Settings
from sluice.providers.mock import MockProvider
from sluice.quality.signals import heuristic_flags, parse_json, repetitive

URL = "/v1/chat/completions"
BODY = {"model": "auto", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 50}


@pytest.mark.parametrize("text,finish,json_,flags", [
    ("Paris.", "stop", False, []),
    ("   ", "stop", False, ["empty"]),
    ("The answer is", "length", False, ["truncated"]),
    ("I'm sorry, but I can't help with that.", "stop", False, ["refusal"]),
    ('{"a": 1', "stop", True, ["invalid_json"]),
    ('```json\n{"a": 1}\n```', "stop", True, []),  # fenced JSON is still usable JSON
    (" ".join(["it is the best"] * 10), "stop", False, ["repetition"]),
])
def test_heuristic_flags(text, finish, json_, flags):
    assert heuristic_flags(text, finish, json_) == flags


def test_parse_json_finds_object_in_prose_and_repetition_needs_length():
    assert parse_json('Sure! Here it is: {"x": [1, 2]} hope that helps') == {"x": [1, 2]}
    assert parse_json("no json here") is None
    assert not repetitive("yes yes yes yes yes")  # too short to call a loop


def auto_app(cheap_text="Paris.", strong=None, **key_kw):
    settings = Settings(
        providers={"cheap": {"type": "mock"}, "strong": {"type": "mock"}},
        routes={"auto": {
            "chain": [{"provider": "cheap", "upstream_model": "small", "retries": 0,
                       "price_in_per_mtok": 1e6, "price_out_per_mtok": 1e6}],
            "escalate": {"chain": [{"provider": "strong", "upstream_model": "big", "retries": 0,
                                    "price_in_per_mtok": 1e7, "price_out_per_mtok": 1e7}]}}})
    cheap = MockProvider("cheap", text=cheap_text)
    strong = strong or MockProvider("strong", text="Canberra is the capital.")
    return make_app(settings, {"cheap": cheap, "strong": strong}, key_kw=key_kw or None)


async def test_good_cheap_answer_is_not_escalated():
    app, raw = auto_app()
    async with client_for(app, raw) as c:
        r = await c.post(URL, json=BODY)
    assert r.headers["x-sluice-provider"] == "cheap" and "x-sluice-escalated" not in r.headers
    assert app.state.ledger.last()["cost_micro_usd"] == 2 * 1_000_000  # 1 prompt + 1 output


async def test_refusal_escalates_and_both_calls_are_billed():
    app, raw = auto_app("I'm sorry, I can't help with that request.")
    async with client_for(app, raw) as c:
        r = await c.post(URL, json=BODY)
    assert r.json()["choices"][0]["message"]["content"] == "Canberra is the capital."
    assert r.headers["x-sluice-escalated"] == "refusal"
    assert r.headers["x-sluice-provider"] == "strong" and r.headers["x-sluice-attempts"] == "2"
    row = app.state.ledger.last()
    # cheap: 1 in + 8 out at $1/tok; strong: 1 in + 4 out at $10/tok
    assert row["cost_micro_usd"] == (1 + 8) * 1_000_000 + (1 + 4) * 10_000_000
    assert (row["prompt_tokens"], row["completion_tokens"]) == (2, 12)
    root = next(s for s in app.state.spans.get_finished_spans() if s.name == "chat auto")
    assert [e.name for e in root.events] == ["sluice.escalate"]


async def test_invalid_json_escalates_only_when_json_was_requested():
    app, raw = auto_app('{"city": "Par')
    async with client_for(app, raw) as c:
        plain = await c.post(URL, json=BODY)
        asked = await c.post(URL, json={**BODY, "response_format": {"type": "json_object"}})
    assert "x-sluice-escalated" not in plain.headers
    assert asked.headers["x-sluice-escalated"] == "invalid_json"


async def test_failed_escalation_keeps_the_cheap_answer_and_does_not_cache_it():
    app, raw = auto_app("I'm sorry, I can't help.", strong=MockProvider("strong", error_rate=1))
    async with client_for(app, raw) as c:
        r = await c.post(URL, json=BODY)
    assert r.status_code == 200 and r.headers["x-sluice-escalated"] == "refusal;failed"
    assert r.json()["choices"][0]["message"]["content"] == "I'm sorry, I can't help."
    assert r.headers["x-sluice-provider"] == "cheap"


async def test_streams_on_an_escalating_route_go_to_the_strong_route():
    app, raw = auto_app()
    async with client_for(app, raw) as c:
        r = await c.post(URL, json={**BODY, "stream": True})
    assert r.headers["x-sluice-provider"] == "strong"
    text = "".join(json.loads(e[6:])["choices"][0]["delta"].get("content", "")
                   for e in r.text.split("\n\n") if e.startswith("data: {")
                   and json.loads(e[6:])["choices"])
    assert text == "Canberra is the capital."


def test_escalate_route_providers_are_validated():
    with pytest.raises(ValueError, match="unknown provider 'nope'"):
        Settings(providers={"a": {"type": "mock"}}, routes={"m": {
            "provider": "a", "upstream_model": "x",
            "escalate": {"provider": "nope", "upstream_model": "y"}}})
