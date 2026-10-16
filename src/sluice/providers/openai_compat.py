"""Adapter for any upstream that already speaks the OpenAI chat-completions format.

Ollama exposes this at /v1, and many hosted providers do too, so one adapter covers them.

Timeouts are httpx's own, never task cancellation: with httpcore 1.0.9, a request cancelled
while its connection is being set up leaves a dead CONNECTING entry in the pool for good.
Enough of them fill the pool and the provider never answers again (bench/httpx_pool_leak.py,
ADR 0007). So each attempt's budget (ChatParams.timeout_s) becomes an httpx.Timeout and the
executor's cancellation is only a late backstop; and a PoolTimeout replaces the client.
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx
from opentelemetry.propagate import inject

from .base import ChatParams, Chunk, Completion, Provider, ProviderError, Usage


class OpenAICompatProvider(Provider):
    enforces_timeout = True
    def __init__(
        self,
        name: str,
        base_url: str,
        api_key: str | None = None,
        timeout_s: float = 60.0,
        client: httpx.AsyncClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.name = name
        self.timeout_s = timeout_s
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

        def make() -> httpx.AsyncClient:
            # connect fast, but allow slow generation between bytes
            return httpx.AsyncClient(base_url=base_url.rstrip("/"), headers=headers,
                                     timeout=httpx.Timeout(timeout_s, connect=5.0),
                                     transport=transport)

        self._make = None if client else make  # an injected client is the caller's to manage
        self._client = client or make()

    def _timeout(self, params: ChatParams):
        if params.timeout_s is None:
            return httpx.USE_CLIENT_DEFAULT
        t = params.timeout_s
        return httpx.Timeout(min(self.timeout_s, t), connect=min(5.0, t))

    async def _reset_if_poisoned(self, client: httpx.AsyncClient) -> None:
        """Every pool slot taken and none freed in time: assume leaked slots, start afresh."""
        if self._make and client is self._client:
            self._client = self._make()
            await client.aclose()

    @staticmethod
    def _body(params: ChatParams, stream: bool) -> dict:
        body: dict = {
            "model": params.model,
            "messages": [{"role": m.role, "content": m.content} for m in params.messages],
            "stream": stream,
        }
        if params.temperature is not None:
            body["temperature"] = params.temperature
        if params.max_tokens is not None:
            body["max_tokens"] = params.max_tokens
        if params.response_format is not None:
            body["response_format"] = params.response_format
        if stream:
            body["stream_options"] = {"include_usage": True}
        return body

    @staticmethod
    def _headers() -> dict[str, str]:
        """W3C `traceparent` for the current (attempt) span. An upstream that is itself traced
        continues our trace; one that isn't (Ollama) just ignores the header."""
        h: dict[str, str] = {}
        inject(h)
        return h

    @staticmethod
    def _usage(raw: dict | None) -> Usage | None:
        if not raw:
            return None
        return Usage(raw.get("prompt_tokens", 0), raw.get("completion_tokens", 0))

    async def complete(self, params: ChatParams) -> Completion:
        client = self._client
        try:
            r = await client.post(
                "/chat/completions", json=self._body(params, False), headers=self._headers(),
                timeout=self._timeout(params))
        except httpx.HTTPError as e:
            if isinstance(e, httpx.PoolTimeout):
                await self._reset_if_poisoned(client)
            raise ProviderError(f"{self.name}: {type(e).__name__}: {e}",
                                timeout=isinstance(e, httpx.TimeoutException)) from e
        if r.status_code != 200:
            raise ProviderError(f"{self.name}: HTTP {r.status_code}: {r.text[:200]}", r.status_code)
        try:
            data = r.json()
            choice = data["choices"][0]
            return Completion(
                content=choice["message"].get("content") or "",
                finish_reason=choice.get("finish_reason") or "stop",
                usage=self._usage(data.get("usage")) or Usage(),
            )
        except (ValueError, KeyError, IndexError, TypeError, AttributeError) as e:
            # a 200 we can't read is an upstream failure, so it must be retryable/failover-able
            raise ProviderError(f"{self.name}: malformed response: {e!r}") from e

    async def stream(self, params: ChatParams) -> AsyncIterator[Chunk]:
        client = self._client
        try:
            async with client.stream(
                "POST", "/chat/completions", json=self._body(params, True), headers=self._headers(),
                timeout=self._timeout(params),
            ) as r:
                if r.status_code != 200:
                    body = (await r.aread()).decode(errors="replace")[:200]
                    raise ProviderError(f"{self.name}: HTTP {r.status_code}: {body}", r.status_code)
                async for line in r.aiter_lines():
                    if not line.startswith("data:"):
                        continue  # blank separators and comments
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        return
                    try:
                        evt = json.loads(payload)
                    except ValueError as e:
                        raise ProviderError(f"{self.name}: malformed SSE event {payload[:80]!r}") from e
                    if "error" in evt:  # in-band error (Ollama and OpenAI both do this mid-stream)
                        raise ProviderError(f"{self.name}: upstream error event: {evt['error']}")
                    usage = self._usage(evt.get("usage"))
                    if not evt.get("choices"):
                        if usage:  # trailing usage-only event
                            yield Chunk(usage=usage)
                        continue
                    choice = evt["choices"][0]
                    yield Chunk(
                        content=choice.get("delta", {}).get("content") or "",
                        finish_reason=choice.get("finish_reason"),
                        usage=usage,
                    )
                # connection closed cleanly but early: without this a truncated answer looks done
                raise ProviderError(f"{self.name}: stream ended before [DONE]")
        except httpx.HTTPError as e:
            if isinstance(e, httpx.PoolTimeout):
                await self._reset_if_poisoned(client)
            raise ProviderError(f"{self.name}: {type(e).__name__}: {e}",
                                timeout=isinstance(e, httpx.TimeoutException)) from e

    async def aclose(self) -> None:
        await self._client.aclose()
