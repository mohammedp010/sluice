from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import redis
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ..auth.keys import KeyStore, VirtualKey
from ..config import (
    Settings,
    build_breakers,
    build_providers,
    build_state,
    load_settings,
)
from ..limits.estimate import cost_micro_usd, estimate_total
from ..limits.state import SharedState
from ..providers.base import ChatParams, Chunk, Message, Provider, ProviderError, Usage
from ..router.executor import AllProvidersFailed, Executor, NonRetryableError, Trail
from .schemas import ChatCompletionRequest

DEFAULT_POLICY = "policies/routes.yaml"
ANON = VirtualKey(id=0, name="anonymous", rpm=10**9, tpm=10**12)  # used only if auth is disabled


def create_app(
    settings: Settings | None = None,
    providers: dict[str, Provider] | None = None,
    executor: Executor | None = None,
    state: SharedState | None = None,
    keystore: KeyStore | None = None,
):
    settings = settings or load_settings(os.environ.get("SLUICE_POLICY", DEFAULT_POLICY))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        for p in app.state.providers.values():
            await p.aclose()
        await app.state.shared.aclose()

    app = FastAPI(title="Sluice", lifespan=lifespan)
    app.state.settings = settings
    app.state.providers = providers or build_providers(settings)
    app.state.shared = state or build_state(settings)
    app.state.keys = keystore or (KeyStore(settings.auth.db_path) if settings.auth.enabled else None)

    paid = frozenset(n for n, c in settings.providers.items() if c.paid)

    async def paid_guard() -> bool:
        """Kill switch for real-money providers: explicit opt-in AND a daily call cap."""
        if os.environ.get("SLUICE_ALLOW_PAID") != "1":
            return False
        return await app.state.shared.incr_paid_calls() <= settings.limits.paid_daily_call_cap

    app.state.executor = executor or Executor(
        app.state.providers, build_breakers(settings), paid_providers=paid, paid_guard=paid_guard)

    @app.middleware("http")
    async def body_size_cap(request: Request, call_next):
        cl = request.headers.get("content-length")
        if cl and cl.isdigit() and int(cl) > settings.auth.max_body_bytes:
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
        # --- 1. who is calling? -------------------------------------------------------------
        key = ANON
        if settings.auth.enabled:
            key = _authenticate(request)
            if key is None:
                return _error(401, "Invalid or missing API key", "invalid_api_key",
                              {"WWW-Authenticate": "Bearer"})
        route = settings.routes.get(req.model)
        if route is None:
            return _error(404, f"The model '{req.model}' does not exist", "model_not_found")
        if not key.allows(req.model):
            return _error(403, f"This key may not use model '{req.model}'", "model_not_allowed")

        sh: SharedState = request.app.state.shared
        ex: Executor = request.app.state.executor

        # --- 2. budget (checked before the call; in-flight requests can overshoot slightly) ---
        day, month = await sh.spend(key.id)
        for spent, cap, label in ((day, key.daily_budget_usd, "daily"),
                                  (month, key.monthly_budget_usd, "monthly")):
            if cap is not None and spent >= cap * 1_000_000:
                return _error(429, f"{label.capitalize()} budget of ${cap} exhausted",
                              "budget_exceeded")

        # --- 3. rate limits: requests/min, then tokens/min (estimated, reconciled later) ------
        messages = [Message(m.role, m.content) for m in req.messages]
        est = estimate_total(messages, req.max_tokens, settings.limits.default_completion_estimate)
        tpm_bucket = f"{key.id}:tpm"
        ok, retry = await sh.acquire(f"{key.id}:rpm", key.rpm, key.rpm, 1)
        if not ok:
            return _rate_limited("requests", retry)
        if est > key.tpm:
            return _error(429, f"Request needs ~{est} tokens, above this key's {key.tpm}/min limit",
                          "rate_limit_exceeded")
        ok, retry = await sh.acquire(tpm_bucket, key.tpm, key.tpm, est)
        if not ok:
            return _rate_limited("tokens", retry)

        # --- 4. do the work ----------------------------------------------------------------
        params = ChatParams(model="", messages=messages, temperature=req.temperature,
                            max_tokens=req.max_tokens)
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
                if micro:
                    await sh.add_spend(key.id, micro)

        try:
            if not req.stream:
                c, trail = await ex.complete(route, params)
                await settle(trail, c.usage)
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
                            "prompt_tokens": c.usage.prompt_tokens,
                            "completion_tokens": c.usage.completion_tokens,
                            "total_tokens": c.usage.total_tokens,
                        },
                    },
                    headers=_trail_headers(trail),
                )
            # Streaming: the executor fails over until the first chunk arrives. After that the
            # status line is sent and failure can only be signalled in-stream (ADR 0001).
            (first, upstream), trail = await ex.stream(route, params)
        except NonRetryableError as e:
            await settle(None, None)  # nothing was produced: refund the whole estimate
            return _error(e.error.status or 502, str(e.error), "upstream_rejected",
                          _trail_headers(e.trail))
        except AllProvidersFailed as e:
            await settle(None, None)
            return _upstream_error(e)

        prompt_est = est - (req.max_tokens or settings.limits.default_completion_estimate)

        async def on_done(usage: Usage | None, emitted_chars: int, failed: bool) -> None:
            if failed:
                ex.report_stream_failure(trail.provider)
            if usage is None:  # no usage reported (error / disconnect): bill what we saw
                usage = Usage(max(prompt_est, 0), emitted_chars // 4)
            await settle(trail, usage)

        return StreamingResponse(
            _sse(cid, created, req.model, first, upstream, on_done),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                     **_trail_headers(trail)},
        )

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
    on_done: Callable[[Usage | None, int, bool], Awaitable[None]],
) -> AsyncIterator[str]:
    usage: Usage | None = None
    chars = 0
    failed = False
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
    except ProviderError as e:
        failed = True
        err = {"error": {"message": str(e), "type": "upstream_error", "code": "stream_interrupted"}}
        yield f"data: {json.dumps(err)}\n\n"  # no [DONE]: the stream did not complete normally
    finally:
        # Runs on success, upstream error AND client disconnect. Shielded so a cancelled request
        # still settles its accounting.
        await asyncio.shield(on_done(usage, chars, failed))


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
