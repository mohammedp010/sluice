"""Adapter for any upstream that already speaks the OpenAI chat-completions format.

Ollama exposes this at /v1, and many hosted providers do too, so one adapter covers them.
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx

from .base import ChatParams, Chunk, Completion, Provider, ProviderError, Usage


class OpenAICompatProvider(Provider):
    def __init__(
        self,
        name: str,
        base_url: str,
        api_key: str | None = None,
        timeout_s: float = 60.0,
        client: httpx.AsyncClient | None = None,
    ):
        self.name = name
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        # connect fast, but allow slow generation between bytes
        timeout = httpx.Timeout(timeout_s, connect=5.0)
        self._client = client or httpx.AsyncClient(
            base_url=base_url.rstrip("/"), headers=headers, timeout=timeout
        )

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
        if stream:
            body["stream_options"] = {"include_usage": True}
        return body

    @staticmethod
    def _usage(raw: dict | None) -> Usage | None:
        if not raw:
            return None
        return Usage(raw.get("prompt_tokens", 0), raw.get("completion_tokens", 0))

    async def complete(self, params: ChatParams) -> Completion:
        try:
            r = await self._client.post("/chat/completions", json=self._body(params, False))
        except httpx.HTTPError as e:
            raise ProviderError(f"{self.name}: {type(e).__name__}: {e}") from e
        if r.status_code != 200:
            raise ProviderError(f"{self.name}: HTTP {r.status_code}: {r.text[:200]}", r.status_code)
        data = r.json()
        choice = data["choices"][0]
        return Completion(
            content=choice["message"].get("content") or "",
            finish_reason=choice.get("finish_reason") or "stop",
            usage=self._usage(data.get("usage")) or Usage(),
        )

    async def stream(self, params: ChatParams) -> AsyncIterator[Chunk]:
        try:
            async with self._client.stream(
                "POST", "/chat/completions", json=self._body(params, True)
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
                    evt = json.loads(payload)
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
        except httpx.HTTPError as e:
            raise ProviderError(f"{self.name}: {type(e).__name__}: {e}") from e

    async def aclose(self) -> None:
        await self._client.aclose()
