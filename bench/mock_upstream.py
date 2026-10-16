"""A deliberately boring OpenAI-compatible upstream for benchmarks: fixed latency, fixed answer.

    python bench/mock_upstream.py --port 9001 --latency-ms 50 [--fail-rate 0.3]

Raw ASGI (no framework) so the upstream itself adds as little as possible. Both gateways under
test call it over real HTTP, so what differs between their numbers is the gateway alone.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random

import uvicorn

TEXT = "This is a fixed benchmark answer from the mock upstream."
CFG = {"latency": 0.05, "fail_rate": 0.0}


async def _json(send, status: int, obj: dict) -> None:
    body = json.dumps(obj).encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


async def app(scope, receive, send):
    if scope["type"] == "lifespan":
        while True:
            msg = await receive()
            if msg["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif msg["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return
    body = b""
    while True:
        msg = await receive()
        body += msg.get("body", b"")
        if not msg.get("more_body"):
            break
    if scope["path"].endswith("/models"):
        return await _json(send, 200, {"object": "list", "data": [{"id": "mock-1",
                                                                     "object": "model"}]})
    req = json.loads(body or b"{}")
    await asyncio.sleep(CFG["latency"])
    if random.random() < CFG["fail_rate"]:
        return await _json(send, 500, {"error": {"message": "injected failure",
                                                 "type": "server_error"}})
    words = TEXT.split(" ")
    usage = {"prompt_tokens": 8, "completion_tokens": len(words), "total_tokens": 8 + len(words)}
    base = {"id": "chatcmpl-mock", "created": 0, "model": req.get("model", "mock-1")}
    if not req.get("stream"):
        return await _json(send, 200, {**base, "object": "chat.completion", "usage": usage,
                                       "choices": [{"index": 0, "finish_reason": "stop",
                                                    "message": {"role": "assistant",
                                                                "content": TEXT}}]})
    await send({"type": "http.response.start", "status": 200,
                "headers": [(b"content-type", b"text/event-stream")]})
    for i, w in enumerate(words):
        chunk = {**base, "object": "chat.completion.chunk", "choices": [{
            "index": 0, "delta": {"content": w + (" " if i < len(words) - 1 else "")},
            "finish_reason": None}]}
        await send({"type": "http.response.body", "body": f"data: {json.dumps(chunk)}\n\n".encode(),
                    "more_body": True})
    end = {**base, "object": "chat.completion.chunk", "usage": usage,
           "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
    await send({"type": "http.response.body",
                "body": f"data: {json.dumps(end)}\n\ndata: [DONE]\n\n".encode()})


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9001)
    ap.add_argument("--latency-ms", type=float, default=50)
    ap.add_argument("--fail-rate", type=float, default=0.0)
    a = ap.parse_args()
    CFG.update(latency=a.latency_ms / 1000, fail_rate=a.fail_rate)
    uvicorn.run(app, host="127.0.0.1", port=a.port, log_level="warning", access_log=False)
