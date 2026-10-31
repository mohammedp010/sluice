"""Runs a request down a route's fallback chain.

Per target: up to (1 + retries) attempts with jittered exponential backoff.
Between targets: fail over. Everything is bounded by the route's overall deadline.
Which errors retry, fail over, or go straight back to the client: see ProviderError and ADR 0002.

Tracing: each upstream attempt is a CLIENT span, a child of whatever span is current (the
gateway's request span). Leaving a target is a `sluice.fallback` event on that parent span.
"""
from __future__ import annotations

import asyncio
import dataclasses
import random
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field

from opentelemetry import trace
from opentelemetry.trace import SpanKind, Status, StatusCode

from ..config import Route, Target
from ..providers.base import ChatParams, Chunk, Completion, Provider, ProviderError
from ..telemetry import semconv as sc
from .breaker import CircuitBreaker


@dataclass
class Attempt:
    provider: str
    outcome: str  # ok | error | timeout | skipped_open | skipped_paid_guard
    ms: float = 0.0
    error: str | None = None
    model: str = ""  # upstream model asked for
    error_type: str | None = None  # low-cardinality reason: "503", "timeout", "ConnectError", ...


@dataclass
class Fallback:
    provider: str  # the target being abandoned
    reason: str  # error_type of its last attempt, or skipped_open / skipped_paid_guard
    next_provider: str | None  # None: it was the last target


@dataclass
class Trail:
    """What happened on the way to the answer. Spans show it live; this copy feeds the response
    headers, error bodies, metrics and the usage ledger."""

    attempts: list[Attempt] = field(default_factory=list)
    fallbacks: list[Fallback] = field(default_factory=list)
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


BACKSTOP_S = 1.0  # extra time before cancelling a provider that enforces its own timeout


def is_timeout(e: BaseException) -> bool:
    return isinstance(e, TimeoutError) or (isinstance(e, ProviderError) and e.timeout)


def error_type(e: BaseException) -> str:
    if is_timeout(e):
        return "timeout"
    if isinstance(e, ProviderError) and e.status is not None:
        return str(e.status)
    cause = e.__cause__  # e.g. httpx.ConnectError, JSONDecodeError
    return type(cause).__name__ if cause else type(e).__name__


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
        tracer: trace.Tracer | None = None,
    ):
        self.paid_providers = paid_providers
        self._paid_guard = paid_guard
        self.providers = providers
        self.breakers = breakers
        self.backoff_base_s = backoff_base_s
        self.backoff_max_s = backoff_max_s
        self._sleep = sleep
        self._rng = rng or random.Random()
        self._tracer = tracer or trace.get_tracer(__name__)  # no-op unless an SDK is set up

    def _backoff(self, n: int) -> float:
        # "full jitter": uniform in [0, min(cap, base * 2^n)] -- avoids synchronized retry storms
        return self._rng.uniform(0, min(self.backoff_max_s, self.backoff_base_s * 2**n))

    def _skip(self, trail: Trail, provider: str, outcome: str) -> None:
        trail.attempts.append(Attempt(provider, outcome, error_type=outcome))

    @staticmethod
    def _leave(trail: Trail, route: Route, i: int) -> None:
        """Record giving up on target i, and why, as an event on the request span."""
        nxt = route.chain[i + 1].provider if i + 1 < len(route.chain) else None
        fb = Fallback(route.chain[i].provider, trail.attempts[-1].error_type or "unknown", nxt)
        trail.fallbacks.append(fb)
        trace.get_current_span().add_event("sluice.fallback", {
            "sluice.provider": fb.provider, sc.ERROR_TYPE: fb.reason,
            "sluice.next_provider": nxt or "none"})

    async def _run(self, route: Route, params: ChatParams, call,
                   skip: frozenset[str] = frozenset()):
        """Generic chain walker. `call(provider, params, timeout)` returns the result or raises.
        Providers in `skip` are passed over (a stream resume never goes back to the one that
        just died)."""
        trail = Trail()
        deadline = time.monotonic() + route.deadline_s
        last: ProviderError | None = None

        for i, target in enumerate(route.chain):
            if target.provider in skip:
                continue
            breaker = self.breakers[target.provider]
            tparams = ChatParams(**{**params.__dict__, "model": target.upstream_model})
            for n in range(target.retries + 1):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AllProvidersFailed(trail, last)
                if not breaker.allow():
                    self._skip(trail, target.provider, "skipped_open")
                    break  # next target
                # after the breaker: a skipped call must not count against the daily paid cap
                if target.provider in self.paid_providers and not (
                    self._paid_guard and await self._paid_guard()
                ):
                    breaker.release()  # no call made: free a half-open probe slot
                    self._skip(trail, target.provider, "skipped_paid_guard")
                    break  # money kill switch: behaves like an open breaker
                result, err = await self._attempt(call, target, tparams, n, remaining, trail)
                if err is None:
                    breaker.record_success()
                    trail.provider = target.provider
                    trail.target = target
                    return result, trail
                if not err.failover:
                    # The request is bad, not the provider: don't punish the breaker.
                    breaker.release()
                    raise NonRetryableError(err, trail) from err
                breaker.record_failure()
                last = err
                if not err.retryable:
                    break  # e.g. bad upstream key: same target won't change, next one might
                if n < target.retries:
                    await self._sleep(min(self._backoff(n), max(deadline - time.monotonic(), 0)))
            self._leave(trail, route, i)
        raise AllProvidersFailed(trail, last)

    async def _attempt(self, call, target: Target, params: ChatParams, n: int, remaining: float,
                       trail: Trail) -> tuple[object, ProviderError | None]:
        """One upstream call inside its own span. Returns (result, None) or (None, error)."""
        attrs = {
            sc.OPERATION_NAME: "chat", sc.PROVIDER_NAME: target.provider,
            sc.REQUEST_MODEL: target.upstream_model, "sluice.attempt": n,
        }
        if params.max_tokens is not None:
            attrs[sc.REQUEST_MAX_TOKENS] = params.max_tokens
        if params.temperature is not None:
            attrs[sc.REQUEST_TEMPERATURE] = params.temperature
        budget = min(target.timeout_s, remaining)
        params = dataclasses.replace(params, timeout_s=budget)
        t0 = time.monotonic()
        with self._tracer.start_as_current_span(
            f"chat {target.upstream_model}", kind=SpanKind.CLIENT, attributes=attrs,
            record_exception=False, set_status_on_exception=False,
        ) as span:
            try:
                result = await call(self.providers[target.provider], params, budget)
            except (ProviderError, TimeoutError) as e:
                err = e if isinstance(e, ProviderError) else ProviderError(
                    f"{target.provider}: timeout after {budget:.2f}s")
                if not isinstance(e, ProviderError):
                    err.__cause__ = e
                et = error_type(e)
                trail.attempts.append(Attempt(
                    target.provider, "timeout" if is_timeout(e) else "error",
                    (time.monotonic() - t0) * 1000, str(err), target.upstream_model, et))
                span.set_attribute(sc.ERROR_TYPE, et)
                span.set_status(Status(StatusCode.ERROR, str(err)[:200]))
                return None, err
            except BaseException:
                self.breakers[target.provider].release()  # cancelled (client left) or a bug
                raise
            trail.attempts.append(Attempt(
                target.provider, "ok", (time.monotonic() - t0) * 1000, model=target.upstream_model))
            return result, None

    def report_stream_failure(self, provider: str) -> None:
        """A stream died after its first chunk. Not retried, but the breaker should learn."""
        self.breakers[provider].record_failure()

    async def complete(self, route: Route, params: ChatParams) -> tuple[Completion, Trail]:
        async def call(p: Provider, prm: ChatParams, timeout: float):
            async with asyncio.timeout(timeout + (BACKSTOP_S if p.enforces_timeout else 0)):
                return await p.complete(prm)

        return await self._run(route, params, call)

    async def stream(
        self, route: Route, params: ChatParams, skip: frozenset[str] = frozenset(),
    ) -> tuple[tuple[Chunk | None, AsyncIterator[Chunk]], Trail]:
        """Fail over until the FIRST chunk arrives (ADR 0001). After that the caller owns the
        stream: a mid-stream error is surfaced, not retried. So a stream's attempt span covers
        connect + time to first chunk; the request span covers the rest."""

        async def call(p: Provider, prm: ChatParams, timeout: float):
            it = p.stream(prm)
            try:
                # time-to-first-chunk budget (a late backstop if the adapter enforces it itself)
                async with asyncio.timeout(timeout + (BACKSTOP_S if p.enforces_timeout else 0)):
                    first = await anext(it)
            except StopAsyncIteration:
                first = None
            except BaseException:
                await it.aclose()
                raise
            return first, it

        return await self._run(route, params, call, skip)
