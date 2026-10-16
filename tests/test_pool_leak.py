"""Regression: attempt timeouts must not leak httpx pool slots (ADR 0007).

Real sockets, an in-process upstream that takes 50 ms. With the executor cancelling httpx
mid-connect, each round of tight timeouts left ~20 dead CONNECTING slots; after five rounds
the pool (100) was full and the provider never answered again.
"""
import asyncio
import socket

import uvicorn

from sluice.config import Route
from sluice.providers.base import ChatParams, Message
from sluice.providers.openai_compat import OpenAICompatProvider
from sluice.router.breaker import CircuitBreaker
from sluice.router.executor import AllProvidersFailed, Executor

BODY = (b'{"choices":[{"message":{"role":"assistant","content":"ok"},"finish_reason":"stop"}],'
        b'"usage":{"prompt_tokens":1,"completion_tokens":1}}')


async def slow_upstream(scope, receive, send):
    if scope["type"] != "http":
        return
    while (await receive()).get("more_body"):
        pass
    await asyncio.sleep(0.05)
    await send({"type": "http.response.start", "status": 200,
                "headers": [(b"content-type", b"application/json")]})
    await send({"type": "http.response.body", "body": BODY})


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def stuck(p: OpenAICompatProvider) -> int:
    return sum("CONNECTING" in repr(c) for c in p._client._transport._pool.connections)


async def test_tight_timeouts_leave_no_dead_pool_slots():
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(slow_upstream, port=port, log_level="error"))
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)
    p = OpenAICompatProvider("up", f"http://127.0.0.1:{port}/v1")
    ex = Executor({"up": p}, {"up": CircuitBreaker(10**9, 1)})
    params = ChatParams("", [Message("user", "hi")])
    try:
        for budget in (0.001, 0.003, 0.01, 0.03) * 2:
            route = Route(deadline_s=5, chain=[{"provider": "up", "upstream_model": "m",
                                                 "retries": 0, "timeout_s": budget}])
            results = await asyncio.gather(*[ex.complete(route, params) for _ in range(150)],
                                           return_exceptions=True)
            assert all(isinstance(r, (tuple, AllProvidersFailed)) for r in results)
        assert stuck(p) == 0
        ok = Route(chain=[{"provider": "up", "upstream_model": "m", "timeout_s": 5}])
        c, _ = await ex.complete(ok, params)
        assert c.content == "ok"
    finally:
        await p.aclose()
        server.should_exit = True
        await task
