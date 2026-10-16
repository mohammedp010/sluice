"""Runs a request down a route's fallback chain.

Per target: up to (1 + retries) attempts with jittered exponential backoff.
Between targets: fail over. Everything is bounded by the route's overall deadline.
Only retryable errors (see ProviderError.retryable) cause a retry or a failover.
"""
from __future__ import annotations

import asyncio
import random
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field

from ..config import Route, Target
from ..providers.base import ChatParams, Chunk, Completion, Provider, ProviderError
from .breaker import CircuitBreaker


@dataclass
class Attempt:
    provider: str
    outcome: str  # ok | error | timeout | skipped_open
    ms: float = 0.0
    error: str | None = None


@dataclass
class Trail:
    """What happened on the way to the answer. Phase 4 turns this into trace spans."""

    attempts: list[Attempt] = field(default_factory=list)
    provider: str | None = None  # who finally served it
    target: Target | None = None


class AllProvidersFailed(Exception):
    def __init__(self, trail: Trail, last: ProviderError | None):
        super().__init__(f"all providers failed: {last}")
        self.trail = trail
        self.last = last


class NonRetryableError(Exception):
    """Upstream rejected the request itself; fail over would not help."""

    def __init__(self, error: ProviderError, trail: Trail):
        super().__init__(str(error))
        self.error = error
        self.trail = trail


class Executor:
    def __init__(
        self,
        providers: dict[str, Provider],
        breakers: dict[str, CircuitBreaker],
        backoff_base_s: float = 0.1,
        backoff_max_s: float = 2.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rng: random.Random | None = None,
        paid_providers: frozenset[str] = frozenset(),
        paid_guard: Callable[[], Awaitable[bool]] | None = None,
    ):
        self.paid_providers = paid_providers
        self._paid_guard = paid_guard
        self.providers = providers
        self.breakers = breakers
        self.backoff_base_s = backoff_base_s
        self.backoff_max_s = backoff_max_s
        self._sleep = sleep
        self._rng = rng or random.Random()

    def _backoff(self, n: int) -> float:
        # "full jitter": uniform in [0, min(cap, base * 2^n)] -- avoids synchronized retry storms
        return self._rng.uniform(0, min(self.backoff_max_s, self.backoff_base_s * 2**n))

    async def _run(self, route: Route, params: ChatParams, call):
        """Generic chain walker. `call(provider, params, timeout)` returns the result or raises."""
        trail = Trail()
        deadline = time.monotonic() + route.deadline_s
        last: ProviderError | None = None

        for target in route.chain:
            breaker = self.breakers[target.provider]
            tparams = ChatParams(**{**params.__dict__, "model": target.upstream_model})
            for n in range(target.retries + 1):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AllProvidersFailed(trail, last)
                if target.provider in self.paid_providers and not (
                    self._paid_guard and await self._paid_guard()
                ):
                    trail.attempts.append(Attempt(target.provider, "skipped_paid_guard"))
                    break  # money kill switch: behaves like an open breaker
                if not breaker.allow():
                    trail.attempts.append(Attempt(target.provider, "skipped_open"))
                    break  # next target
                t0 = time.monotonic()
                try:
                    result = await call(
                        self.providers[target.provider], tparams, min(target.timeout_s, remaining)
                    )
                except (ProviderError, TimeoutError) as e:
                    err = e if isinstance(e, ProviderError) else ProviderError(
                        f"{target.provider}: timeout after {target.timeout_s}s")
                    ms = (time.monotonic() - t0) * 1000
                    trail.attempts.append(Attempt(
                        target.provider, "timeout" if isinstance(e, TimeoutError) else "error",
                        ms, str(err)))
                    if not err.failover:
                        # The request is bad, not the provider: don't punish the breaker.
                        breaker.release()
                        raise NonRetryableError(err, trail) from e
                    breaker.record_failure()
                    last = err
                    if not err.retryable:
                        break  # e.g. bad upstream key: same target won't change, next one might
                    if n < target.retries:
                        await self._sleep(min(self._backoff(n), max(deadline - time.monotonic(), 0)))
                    continue
                except BaseException:
                    breaker.release()  # cancelled (client left) or a bug: no verdict on health
                    raise
                breaker.record_success()
                trail.attempts.append(Attempt(target.provider, "ok", (time.monotonic() - t0) * 1000))
                trail.provider = target.provider
                trail.target = target
                return result, trail
        raise AllProvidersFailed(trail, last)

    def report_stream_failure(self, provider: str) -> None:
        """A stream died after its first chunk. Not retried, but the breaker should learn."""
        self.breakers[provider].record_failure()

    async def complete(self, route: Route, params: ChatParams) -> tuple[Completion, Trail]:
        async def call(p: Provider, prm: ChatParams, timeout: float):
            async with asyncio.timeout(timeout):
                return await p.complete(prm)

        return await self._run(route, params, call)

    async def stream(
        self, route: Route, params: ChatParams
    ) -> tuple[tuple[Chunk | None, AsyncIterator[Chunk]], Trail]:
        """Fail over until the FIRST chunk arrives (ADR 0001). After that the caller owns the
        stream: a mid-stream error is surfaced, not retried."""

        async def call(p: Provider, prm: ChatParams, timeout: float):
            it = p.stream(prm)
            try:
                async with asyncio.timeout(timeout):  # time-to-first-chunk budget
                    first = await anext(it)
            except StopAsyncIteration:
                first = None
            except BaseException:
                await it.aclose()
                raise
            return first, it

        return await self._run(route, params, call)
