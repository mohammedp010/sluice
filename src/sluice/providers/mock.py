"""A fake upstream with knobs. Free, deterministic, and the backbone of later chaos tests."""
from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncIterator

from .base import ChatParams, Chunk, Completion, Provider, ProviderError, Usage


class MockProvider(Provider):
    def __init__(
        self,
        name: str = "mock",
        text: str = "This is a mock response from Sluice.",
        latency_ms: float = 0,
        tokens_per_sec: float | None = None,
        error_rate: float = 0.0,
        error_status: int = 500,
        cut_stream_after: int | None = None,
        seed: int | None = None,
    ):
        self.name = name
        self.text = text
        self.latency_ms = latency_ms
        self.tokens_per_sec = tokens_per_sec
        self.error_rate = error_rate
        self.error_status = error_status
        self.cut_stream_after = cut_stream_after
        self._rng = random.Random(seed)

    def _tokens(self) -> list[str]:
        words = self.text.split(" ")
        return [w + (" " if i < len(words) - 1 else "") for i, w in enumerate(words)]

    @staticmethod
    def _prompt_tokens(params: ChatParams) -> int:
        return sum(len(m.content.split()) for m in params.messages)

    async def _maybe_fail(self) -> None:
        if self.latency_ms:
            await asyncio.sleep(self.latency_ms / 1000)
        if self._rng.random() < self.error_rate:
            raise ProviderError(f"{self.name}: injected failure", status=self.error_status)

    async def complete(self, params: ChatParams) -> Completion:
        await self._maybe_fail()
        toks = self._tokens()
        if params.max_tokens is not None:
            toks = toks[: params.max_tokens]
        finish = "length" if params.max_tokens is not None and len(toks) < len(self._tokens()) else "stop"
        return Completion(
            content="".join(toks),
            finish_reason=finish,
            usage=Usage(self._prompt_tokens(params), len(toks)),
        )

    async def stream(self, params: ChatParams) -> AsyncIterator[Chunk]:
        await self._maybe_fail()
        toks = self._tokens()
        if params.max_tokens is not None:
            toks = toks[: params.max_tokens]
        delay = 1 / self.tokens_per_sec if self.tokens_per_sec else 0
        for i, tok in enumerate(toks):
            if self.cut_stream_after is not None and i >= self.cut_stream_after:
                raise ProviderError(f"{self.name}: stream cut", status=None)
            if delay:
                await asyncio.sleep(delay)
            yield Chunk(content=tok)
        yield Chunk(
            finish_reason="stop",
            usage=Usage(self._prompt_tokens(params), len(toks)),
        )
