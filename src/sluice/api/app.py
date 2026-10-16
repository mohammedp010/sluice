from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass

import redis
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from opentelemetry import trace
from opentelemetry.propagate import extract
from opentelemetry.trace import Span, SpanKind, Status, StatusCode

from ..auth.keys import KeyStore, VirtualKey
from ..config import (
    Settings,
    build_breakers,
    build_providers,
    build_state,
    load_settings,
)
from ..limits.estimate import cost_micro_usd, estimate_prompt_tokens
from ..limits.state import SharedState
from ..providers.base import ChatParams, Chunk, Message, Provider, ProviderError, Usage
from ..router.executor import AllProvidersFailed, Executor, NonRetryableError, Trail
from ..telemetry import semconv as sc
from ..telemetry.ledger import UsageLedger
from ..telemetry.otel import Metrics, Telemetry, build_telemetry
from .schemas import ChatCompletionRequest

DEFAULT_POLICY = "policies/routes.yaml"
ANON = VirtualKey(id=0, name="anonymous", rpm=10**9, tpm=10**12)  # used only if auth is disabled


@dataclass
class RequestLog:
    """What we learned about one request. Written once, at the very end, to the request span,
    the metrics and the usage ledger. For streams "the end" is the last byte, not the handler
    returning."""

    span: Span
    t0: float
    model: str  # public model name
    key: VirtualKey | None = None
    trail: Trail | None = None
    usage: Usage | None = None
    cost_micro: int = 0
    finish_reason: str | None = None
    status: int = 200
    outcome: str = "ok"  # ok | an error code | stream_interrupted | client_disconnected
    done: bool = False


def create_app(
    settings: Settings | None = None,
    providers: dict[str, Provider] | None = None,
    executor: Executor | None = None,
    state: SharedState | None = None,
    keystore: KeyStore | None = None,
    telemetry: Telemetry | None = None,
    ledger: UsageLedger | None = None,
):
    settings = settings or load_settings(os.environ.get("SLUICE_POLICY", DEFAULT_POLICY))
    tcfg = settings.telemetry
    tel = telemetry or build_telemetry(
        tcfg.service_name, tcfg.traces, tcfg.otlp_endpoint, tcfg.prometheus)
    if ledger is None and tcfg.ledger:
        ledger = UsageLedger(settings.auth.db_path)
    tracer = tel.tracer()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        for p in app.state.providers.values():
            await p.aclose()
        await app.state.shared.aclose()
        tel.shutdown()

    app = FastAPI(title="Sluice", lifespan=lifespan)
    app.state.settings = settings
    app.state.providers = providers or build_providers(settings)
    app.state.shared = state or build_state(settings)
    app.state.keys = keystore or (KeyStore(settings.auth.db_path) if settings.auth.enabled else None)
    app.state.telemetry = tel
    app.state.ledger = ledger

    paid = frozenset(n for n, c in settings.providers.items() if c.paid)

    async def paid_guard() -> bool:
        """Kill switch for real-money providers: explicit opt-in AND a daily call cap."""
        if os.environ.get("SLUICE_ALLOW_PAID") != "1":
            return False
        return await app.state.shared.incr_paid_calls() <= settings.limits.paid_daily_call_cap

    app.state.executor = executor or Executor(
        app.state.providers, build_breakers(settings), paid_providers=paid, paid_guard=paid_guard,
        tracer=tracer)
    metrics = Metrics(
        tel.meter(), lambda: {n: b.state.value for n, b in app.state.executor.breakers.items()})

    @app.middleware("http")
    async def body_size_cap(request: Request, call_next):
        if request.method == "POST":
            cl = request.headers.get("content-length")
            if cl is None:  # chunked upload: size unknown up front, so the cap can't be enforced
                return _error(411, "Content-Length required", "length_required")
            if cl.isdigit() and int(cl) > settings.auth.max_body_bytes:
                return _error(413, "Request body too large", "request_too_large")
        return await call_next(request)

    @app.exception_handler(redis.RedisError)
    async def state_store_down(request: Request, exc: redis.RedisError):
        # Fail closed: without shared state we can't enforce limits or budgets, so refuse
        # rather than silently allow unmetered spend. (ADR 0003)
        return _error(503, "Limit store unavailable", "limiter_unavailable", {"Retry-After": "5"})

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    @app.get("/v1/sluice/health")
    async def provider_health(request: Request):
        return {n: b.state.value for n, b in request.app.state.executor.breakers.items()}

    if tel.prometheus:
        from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

        @app.get("/metrics")
        async def prometheus_metrics():
            return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.get("/v1/models")
    async def list_models(request: Request):
        now = int(time.time())
        return {
            "object": "list",
            "data": [
                {"id": m, "object": "model", "created": now, "owned_by": r.chain[0].provider}
                for m, r in request.app.state.settings.routes.items()
            ],
        }

    @app.post("/v1/chat/completions")
    async def chat_completions(req: ChatCompletionRequest, request: Request):
        # The request span. `extract` continues the caller's trace if it sent a traceparent.
        span = tracer.start_span(
            f"chat {req.model}", context=extract(request.headers), kind=SpanKind.SERVER,
            attributes={sc.OPERATION_NAME: "chat", sc.REQUEST_MODEL: req.model,
                        "sluice.stream": req.stream})
        log = RequestLog(span, time.monotonic(), req.model)
        try:
            with trace.use_span(span, end_on_exit=False):
                resp = await handle(req, request, log)
        except Exception as e:
            log.status, log.outcome = ((503, "limiter_unavailable") if isinstance(e, redis.RedisError)
                                       else (500, "internal_error"))
            span.record_exception(e)
            finish(log)
            raise
        resp.headers["x-sluice-trace-id"] = format(span.get_span_context().trace_id, "032x")
        if not isinstance(resp, StreamingResponse):  # streams finish in their on_done
            log.status = resp.status_code
            if resp.status_code >= 400:
                log.outcome = json.loads(resp.body)["error"]["code"]
            finish(log)
        return resp

    async def handle(req: ChatCompletionRequest, request: Request, log: RequestLog) -> Response:
        # --- 1. who is calling? -------------------------------------------------------------
        with tracer.start_as_current_span("sluice.auth"):
            key = _authenticate(request) if settings.auth.enabled else ANON
        if key is None:
            return _error(401, "Invalid or missing API key", "invalid_api_key",
                          {"WWW-Authenticate": "Bearer"})
        log.key = key
        log.span.set_attributes({"sluice.key.id": key.id, "sluice.team": key.team,
                                 "sluice.app": key.app})
        route = settings.routes.get(req.model)
        if route is None:
            return _error(404, f"The model '{req.model}' does not exist", "model_not_found")
        if not key.allows(req.model):
            return _error(403, f"This key may not use model '{req.model}'", "model_not_allowed")

        sh: SharedState = request.app.state.shared
        ex: Executor = request.app.state.executor
        messages = [Message(m.role, m.content) for m in req.messages]
        max_tokens = req.max_tokens or req.max_completion_tokens
        prompt_est = estimate_prompt_tokens(messages)
        est = prompt_est + (max_tokens or settings.limits.default_completion_estimate)
        tpm_bucket = f"{key.id}:tpm"

        with tracer.start_as_current_span("sluice.limits", attributes={"sluice.tokens.estimate": est}):
            # --- 2. budget (checked before the call; in-flight requests can overshoot slightly)
            day, month = await sh.spend(key.id)
            for spent, cap, label in ((day, key.daily_budget_usd, "daily"),
                                      (month, key.monthly_budget_usd, "monthly")):
                if cap is not None and spent >= cap * 1_000_000:
                    return _error(429, f"{label.capitalize()} budget of ${cap} exhausted",
                                  "budget_exceeded")

            # --- 3. rate limits: requests/min, then tokens/min (estimated, reconciled later) ---
            ok, retry = await sh.acquire(f"{key.id}:rpm", key.rpm, key.rpm, 1)
            if not ok:
                return _rate_limited("requests", retry)
            if est > key.tpm:
                return _error(429, f"Request needs ~{est} tokens, above this key's {key.tpm}/min "
                              "limit", "rate_limit_exceeded")
            ok, retry = await sh.acquire(tpm_bucket, key.tpm, key.tpm, est)
            if not ok:
                return _rate_limited("tokens", retry)

        with tracer.start_as_current_span("sluice.cache", attributes={"sluice.cache.result":
                                                                      "disabled"}):
            pass  # Phase 5

        # --- 4. do the work ----------------------------------------------------------------
        params = ChatParams(model="", messages=messages, temperature=req.temperature,
                            max_tokens=max_tokens or settings.limits.default_max_tokens)
        cid = f"chatcmpl-{uuid.uuid4().hex[:24]}"
        created = int(time.time())

        async def settle(trail: Trail | None, usage: Usage | None) -> None:
            """Reconcile the estimate with reality: fix the TPM bucket and record spend."""
            actual = usage.total_tokens if usage else 0
            await sh.adjust(tpm_bucket, key.tpm, est - actual)
            if usage and trail and trail.target:
                t = trail.target
                micro = cost_micro_usd(usage.prompt_tokens, usage.completion_tokens,
                                       t.price_in_per_mtok, t.price_out_per_mtok)
                log.usage, log.cost_micro = usage, micro
                if micro:
                    await sh.add_spend(key.id, micro)

        try:
            if not req.stream:
                c, trail = await ex.complete(route, params)
                log.trail, log.finish_reason = trail, c.finish_reason
                usage = c.usage
                if not usage.total_tokens:  # upstream reported no usage: bill an estimate, not 0
                    usage = Usage(prompt_est, len(c.content) // 4)
                await settle(trail, usage)
                return JSONResponse(
                    {
                        "id": cid, "object": "chat.completion", "created": created,
                        "model": req.model,
                        "choices": [{
                            "index": 0,
                            "message": {"role": "assistant", "content": c.content},
                            "finish_reason": c.finish_reason,
                        }],
                        "usage": {
                            "prompt_tokens": usage.prompt_tokens,
                            "completion_tokens": usage.completion_tokens,
                            "total_tokens": usage.total_tokens,
                        },
                    },
                    headers=_trail_headers(trail),
                )
            # Streaming: the executor fails over until the first chunk arrives. After that the
            # status line is sent and failure can only be signalled in-stream (ADR 0001).
            (first, upstream), trail = await ex.stream(route, params)
            log.trail = trail
        except NonRetryableError as e:
            log.trail = e.trail
            await settle(None, None)  # nothing was produced: refund the whole estimate
            return _error(e.error.status or 502, str(e.error), "upstream_rejected",
                          _trail_headers(e.trail))
        except AllProvidersFailed as e:
            log.trail = e.trail
            await settle(None, None)
            return _upstream_error(e)

        async def on_done(usage: Usage | None, emitted_chars: int, outcome: str) -> None:
            try:
                if outcome == "stream_interrupted":
                    ex.report_stream_failure(trail.provider)
                if usage is None:  # no usage reported (error / disconnect): bill what we saw
                    usage = Usage(prompt_est, emitted_chars // 4)
                await settle(trail, usage)
            finally:
                log.outcome = outcome
                finish(log)

        return StreamingResponse(
            _sse(cid, created, req.model, first, upstream, on_done),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                     **_trail_headers(trail)},
        )

    def finish(log: RequestLog) -> None:
        """Close the request span and emit metrics + the ledger row. Exactly once."""
        if log.done:
            return
        log.done = True
        span, trail, usage = log.span, log.trail, log.usage
        provider = (trail.provider if trail else None) or "none"
        upstream_model = trail.target.upstream_model if trail and trail.target else None
        span.set_attributes({"http.response.status_code": log.status,
                             "sluice.outcome": log.outcome, "sluice.provider": provider})
        failovers = [f for f in trail.fallbacks if f.next_provider] if trail else []
        if trail:
            span.set_attributes({"sluice.attempts": len(trail.attempts),
                                 "sluice.fallbacks": len(failovers)})
            for a in trail.attempts:
                if a.outcome.startswith("skipped"):
                    continue  # never called: no duration to report
                attrs = {sc.OPERATION_NAME: "chat", sc.PROVIDER_NAME: a.provider,
                         sc.REQUEST_MODEL: a.model}
                if a.error_type:
                    attrs[sc.ERROR_TYPE] = a.error_type
                metrics.attempt_duration.record(a.ms / 1000, attrs)
            for f in failovers:
                metrics.fallbacks.add(1, {"sluice.provider": f.provider, sc.ERROR_TYPE: f.reason})
        if upstream_model:
            span.set_attribute(sc.RESPONSE_MODEL, upstream_model)
        if log.finish_reason:
            span.set_attribute(sc.RESPONSE_FINISH_REASONS, [log.finish_reason])
        if usage:
            span.set_attributes({sc.USAGE_INPUT_TOKENS: usage.prompt_tokens,
                                 sc.USAGE_OUTPUT_TOKENS: usage.completion_tokens,
                                 "sluice.cost_usd": log.cost_micro / 1e6})
            base = {sc.OPERATION_NAME: "chat", sc.PROVIDER_NAME: provider,
                    sc.REQUEST_MODEL: upstream_model or ""}
            metrics.tokens.record(usage.prompt_tokens, {**base, sc.TOKEN_TYPE: "input"})
            metrics.tokens.record(usage.completion_tokens, {**base, sc.TOKEN_TYPE: "output"})
            if log.cost_micro:
                metrics.cost.add(log.cost_micro / 1e6,
                                 {sc.REQUEST_MODEL: log.model, "sluice.provider": provider})
        if log.status >= 500 or log.outcome == "stream_interrupted":
            span.set_status(Status(StatusCode.ERROR, log.outcome))
        secs = time.monotonic() - log.t0
        metrics.requests.add(1, {sc.REQUEST_MODEL: log.model, "sluice.provider": provider,
                                 "sluice.outcome": log.outcome,
                                 "http.response.status_code": log.status})
        metrics.duration.record(secs, {sc.REQUEST_MODEL: log.model, "sluice.outcome": log.outcome})
        if ledger and trail and log.key:  # only requests that reached a provider cost anything
            try:
                ledger.record(
                    trace_id=format(span.get_span_context().trace_id, "032x"),
                    key_id=log.key.id, key_name=log.key.name, team=log.key.team,
                    app=log.key.app, model=log.model, provider=provider,
                    upstream_model=upstream_model, status=log.status, outcome=log.outcome,
                    attempts=len(trail.attempts), fallbacks=len(failovers),
                    prompt_tokens=usage.prompt_tokens if usage else 0,
                    completion_tokens=usage.completion_tokens if usage else 0,
                    cost_micro_usd=log.cost_micro, latency_ms=round(secs * 1000, 1))
            except sqlite3.Error as e:  # ledger trouble must not fail a served request
                span.record_exception(e)
        span.end()

    def _authenticate(request: Request) -> VirtualKey | None:
        auth = request.headers.get("authorization", "")
        if not auth.lower().startswith("bearer "):
            return None
        return request.app.state.keys.lookup(auth[7:].strip())

    return app


def _frame(cid: str, created: int, model: str, delta: dict, finish: str | None) -> str:
    obj = {
        "id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    return f"data: {json.dumps(obj)}\n\n"


async def _sse(
    cid: str, created: int, model: str, first: Chunk | None, rest: AsyncIterator[Chunk],
    on_done: Callable[[Usage | None, int, str], Awaitable[None]],
) -> AsyncIterator[str]:
    usage: Usage | None = None
    chars = 0
    outcome = "client_disconnected"  # until the stream proves otherwise
    try:
        yield _frame(cid, created, model, {"role": "assistant", "content": ""}, None)
        async for chunk in _chain([first] if first else [], rest):
            if chunk.usage:
                usage = chunk.usage
            chars += len(chunk.content)
            if chunk.content or chunk.finish_reason:
                delta = {"content": chunk.content} if chunk.content else {}
                yield _frame(cid, created, model, delta, chunk.finish_reason)
        if usage:
            tail = {
                "id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
                "choices": [],
                "usage": {
                    "prompt_tokens": usage.prompt_tokens,
                    "completion_tokens": usage.completion_tokens,
                    "total_tokens": usage.total_tokens,
                },
            }
            yield f"data: {json.dumps(tail)}\n\n"
        yield "data: [DONE]\n\n"
        outcome = "ok"
    except ProviderError as e:
        outcome = "stream_interrupted"
        err = {"error": {"message": str(e), "type": "upstream_error", "code": "stream_interrupted"}}
        yield f"data: {json.dumps(err)}\n\n"  # no [DONE]: the stream did not complete normally
    finally:
        # Runs on success, upstream error AND client disconnect. Close the upstream right away
        # (otherwise it keeps generating until GC), then settle. Shielded so a cancelled request
        # still settles its accounting.
        async def finish() -> None:
            await rest.aclose()
            await on_done(usage, chars, outcome)

        await asyncio.shield(finish())


async def _chain(head: list[Chunk], tail: AsyncIterator[Chunk]) -> AsyncIterator[Chunk]:
    for h in head:
        yield h
    async for t in tail:
        yield t


def _trail_headers(trail: Trail) -> dict[str, str]:
    return {
        "x-sluice-provider": trail.provider or "",
        "x-sluice-attempts": str(len(trail.attempts)),
    }


def _error(status: int, message: str, code: str, headers: dict | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"message": message, "type": "invalid_request_error", "code": code}},
        headers=headers,
    )


def _rate_limited(what: str, retry_after: float) -> JSONResponse:
    secs = max(1, int(retry_after + 0.999))
    return _error(429, f"Rate limit reached for {what}. Retry in {secs}s.", "rate_limit_exceeded",
                  {"Retry-After": str(secs)})


def _upstream_error(e: AllProvidersFailed) -> JSONResponse:
    timed_out = bool(e.trail.attempts) and all(
        a.outcome in ("timeout", "skipped_open", "skipped_paid_guard") for a in e.trail.attempts)
    return JSONResponse(
        status_code=504 if timed_out else 502,
        content={"error": {
            "message": str(e),
            "type": "upstream_error",
            "code": "all_providers_failed",
            "attempts": [a.__dict__ for a in e.trail.attempts],
        }},
        headers=_trail_headers(e.trail),
    )


def app_factory():
    return create_app()
