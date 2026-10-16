"""The OpenAI-compatible adapter, tested against a fake upstream (no network, no cost)."""
import json

import httpx
import pytest

from sluice.providers.base import ChatParams, Message, ProviderError
from sluice.providers.openai_compat import OpenAICompatProvider

PARAMS = ChatParams(model="x", messages=[Message("user", "hi")])


def provider(handler) -> OpenAICompatProvider:
    client = httpx.AsyncClient(base_url="http://up/v1", transport=httpx.MockTransport(handler))
    return OpenAICompatProvider("up", "http://up/v1", client=client)


async def test_complete_parses_response():
    def h(req):
        assert json.loads(req.content)["stream"] is False
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "yo"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 1}})
    c = await provider(h).complete(PARAMS)
    assert (c.content, c.usage.total_tokens) == ("yo", 3)


async def test_non_200_becomes_provider_error_with_status():
    p = provider(lambda r: httpx.Response(429, text="slow down"))
    with pytest.raises(ProviderError) as e:
        await p.complete(PARAMS)
    assert e.value.status == 429


async def test_stream_parses_sse():
    def ev(d):
        return f"data: {json.dumps(d)}\n\n"
    body = (
        ev({"choices": [{"delta": {"content": "a"}, "finish_reason": None}]})
        + ev({"choices": [{"delta": {"content": "b"}, "finish_reason": "stop"}]})
        + ev({"choices": [], "usage": {"prompt_tokens": 1, "completion_tokens": 2}})
        + "data: [DONE]\n\n"
    )
    p = provider(lambda r: httpx.Response(200, text=body))
    chunks = [c async for c in p.stream(PARAMS)]
    assert "".join(c.content for c in chunks) == "ab"
    assert chunks[-1].usage.completion_tokens == 2


async def test_connection_failure_is_provider_error():
    def boom(req):
        raise httpx.ConnectError("refused")
    with pytest.raises(ProviderError):
        await provider(boom).complete(PARAMS)


async def test_malformed_200_is_retryable_provider_error():
    p = provider(lambda r: httpx.Response(200, text="<html>proxy error</html>"))
    with pytest.raises(ProviderError) as e:
        await p.complete(PARAMS)
    assert e.value.retryable


@pytest.mark.parametrize("tail", [
    'data: {"error": {"message": "model crashed"}}\n\n',  # in-band error event
    "",                                                     # connection closed without [DONE]
])
async def test_stream_errors_are_not_silent_success(tail):
    body = 'data: {"choices": [{"delta": {"content": "a"}, "finish_reason": null}]}\n\n' + tail
    p = provider(lambda r: httpx.Response(200, text=body))
    with pytest.raises(ProviderError):
        [c async for c in p.stream(PARAMS)]
