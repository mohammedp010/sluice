from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import os
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

from ..auth.keys import KeyStore, VirtualKey, client_ip, parse_networks
from ..cache.semantic import CachedAnswer, Probe, SemanticCache
from ..config import (
    Settings,
    Target,
    build_breakers,
    build_cache,
    build_providers,
    build_state,
    load_settings,
)
from ..guard.secrets import scan
from ..guard.watch import KEY_SHAPED, Watch
from ..limits.estimate import cost_micro_usd, estimate_prompt_tokens
from ..limits.state import SharedState
from ..mcp.audit import McpAudit
from ..mcp.pins import ToolPins
from ..mcp.proxy import PARSE_ERROR, SUPPORTED, McpProxy, rpc_error
from ..mcp.upstream import Upstreams
from ..providers.base import (
    ChatParams,
    Chunk,
    Completion,
    Message,
    Provider,
    ProviderError,
    Usage,
)
from ..quality.signals import heuristic_flags
from ..router.executor import AllProvidersFailed, Executor, NonRetryableError, Trail
from ..router.resume import ResumableStream
from ..telemetry import semconv as sc
from ..telemetry.ledger import UsageLedger
from ..telemetry.otel import Metrics, Telemetry, build_telemetry
from ..telemetry.recent import RecentTraces
from .admin import admin_router
from .schemas import ChatCompletionRequest

DEFAULT_POLICY = "policies/routes.yaml"
ANON = VirtualKey(id=0, name="anonymous", rpm=10**9, tpm=10**12, tool_rpm=10**9)  # used only if auth is disabled


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
    cache: str | None = None  # hit | miss | skip, once the cache stage ran
    done: bool = False


@dataclass
class StreamResult:
    """How a stream ended, handed to on_done by _sse."""

    usage: Usage | None
    text: str
    finish_reason: str | None
    outcome: str  # ok | stream_interrupted | client_disconnected


def create_app(
    settings: Settings | None = None,
    providers: dict[str, Provider] | None = None,
    executor: Executor | None = None,
    state: SharedState | None = None,
    keystore: KeyStore | None = None,
    telemetry: Telemetry | None = None,
    ledger: UsageLedger | None = None,
    cache: SemanticCache | None = None,
    mcp_audit: McpAudit | None = None,
    tool_pins: ToolPins | None = None,
):
    settings = settings or load_settings(os.environ.get("SLUICE_POLICY", DEFAULT_POLICY))
    tcfg = settings.telemetry
    tel = telemetry or build_telemetry(
        tcfg.service_name, tcfg.traces, tcfg.otlp_endpoint, tcfg.prometheus)
    if ledger is None and tcfg.ledger:
        ledger = UsageLedger(settings.auth.db_path)
    tracer = tel.tracer()
    cache = cache or build_cache(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        flusher = asyncio.create_task(ledger.run_flusher()) if ledger else None
        pruner = (asyncio.create_task(_prune_audit(app.state.mcp.audit, days))
                  if (days := settings.mcp.audit_retention_days) else None)
        yield
        if pruner:
            pruner.cancel()
        if flusher:
            flusher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await flusher  # its finally block writes what's left
        for p in app.state.providers.values():
            await p.aclose()
        await app.state.shared.aclose()
        if cache and hasattr(cache.embedder, "aclose"):
            await cache.embedder.aclose()
        await upstreams.close()
        tel.shutdown()

    # No /docs, /redoc or /openapi.json: they would map every route, admin included, for anyone
    # who can reach the port. The API is OpenAI's, documented elsewhere. (ADR 0010)
    app = FastAPI(title="Sluice", lifespan=lifespan, docs_url=None, redoc_url=None,
                  openapi_url=None)
    app.state.settings = settings
    app.state.providers = providers or build_providers(settings)
    app.state.shared = state or build_state(settings)
    app.state.keys = keystore or (KeyStore(settings.auth.db_path) if settings.auth.enabled else None)
    app.state.telemetry = tel
    app.state.ledger = ledger
    app.state.cache = cache
    mc = settings.mcp
    upstreams = Upstreams(mc.servers, mc.tools_ttl_s, pins=tool_pins or ToolPins(
        mc.audit_db or settings.auth.db_path, mc.pin_mode, mc.lint_max_description))

    paid = frozenset(n for n, c in settings.providers.items() if c.paid)

    async def paid_guard() -> bool:
        """Kill switch for real-money providers: explicit opt-in AND a daily call cap."""
        if os.environ.get("SLUICE_ALLOW_PAID") != "1":
            return False
        keys = app.state.keys
        if keys is not None and await asyncio.to_thread(keys.flag, "paid_off"):
            return False  # `sluice.admin lockdown` (ADR 0012)
        return await app.state.shared.incr_paid_calls() <= settings.limits.paid_daily_call_cap

    app.state.executor = executor or Executor(
        app.state.providers, build_breakers(settings), paid_providers=paid, paid_guard=paid_guard,
        tracer=tracer)
    trusted_proxies = parse_networks(settings.auth.trusted_proxies)
    metrics = Metrics(
        tel.meter(), lambda: {n: b.state.value for n, b in app.state.executor.breakers.items()})
    watch = app.state.watch = Watch(settings.watch, app.state.shared, app.state.keys, metrics)
    app.state.mcp = McpProxy(upstreams, mcp_audit or McpAudit(
        mc.audit_db or settings.auth.db_path), tracer, settings.guard, metrics, watch,
        app.state.shared)

    if settings.admin.enabled:  # read-only API for the UI; also keeps the last N traces
        recent = RecentTraces(settings.admin.recent_traces)
        tel.tracer_provider.add_span_processor(recent)
        app.include_router(admin_router(recent, settings.admin.token_env, trusted_proxies))

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

    @app.post("/mcp")
    async def mcp_endpoint(request: Request):
        """MCP over Streamable HTTP (stateless, JSON responses). See sluice/mcp/proxy.py."""
        origin = request.headers.get("origin")
        if origin and origin not in settings.mcp.allowed_origins:
            return _error(403, "Origin not allowed", "origin_not_allowed")
        version = request.headers.get("mcp-protocol-version")
        if version and version not in SUPPORTED:
            return _error(400, f"Unsupported MCP protocol version {version}",
                          "unsupported_protocol_version")
        key = (await _authenticate(request)) if settings.auth.enabled else ANON
        if key is None:
            return _error(401, "Invalid or missing API key", "invalid_api_key",
                          {"WWW-Authenticate": "Bearer"})
        try:
            msg = json.loads(await request.body())
        except ValueError:
            return JSONResponse(rpc_error(None, PARSE_ERROR, "invalid JSON"), status_code=400)
        reply = await request.app.state.mcp.handle(msg, key)
        return Response(status_code=202) if reply is None else JSONResponse(reply)

    @app.api_route("/mcp", methods=["GET", "DELETE"])
    async def mcp_no_stream():
        # stateless: no server-initiated stream, no session to delete (both allowed by the spec)
        return Response(status_code=405, headers={"Allow": "POST"})

    @app.get("/v1/models")
    async def list_models(request: Request):
        # Needs a key, like OpenAI's, and lists only what that key may use: route and provider
        # names are a map for anyone probing the port. (ADR 0010)
        key = (await _authenticate(request)) if settings.auth.enabled else ANON
        if key is None:
            return _error(401, "Invalid or missing API key", "invalid_api_key",
                          {"WWW-Authenticate": "Bearer"})
        now = int(time.time())
        return {
            "object": "list",
            "data": [
                {"id": m, "object": "model", "created": now, "owned_by": r.chain[0].provider}
                for m, r in request.app.state.settings.routes.items() if key.allows(m)
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
        if log.cache:
            resp.headers["x-sluice-cache"] = log.cache
        if not isinstance(resp, StreamingResponse):  # streams finish in their on_done
            log.status = resp.status_code
            if resp.status_code >= 400:
                log.outcome = json.loads(resp.body)["error"]["code"]
            finish(log)
        return resp

    async def handle(req: ChatCompletionRequest, request: Request, log: RequestLog) -> Response:
        # --- 1. who is calling? -------------------------------------------------------------
        with tracer.start_as_current_span("sluice.auth"):
            key = (await _authenticate(request)) if settings.auth.enabled else ANON
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
        if settings.guard.chat != "off":  # secrets about to leave for a provider (ADR 0012)
            found = [f for i, m in enumerate(req.messages)
                     for f in scan(m.content, f"messages[{i}]")]
            if found:
                action = "block" if settings.guard.chat == "block" else "audit"
                for f in found:
                    metrics.guard_findings.add(1, {"sluice.guard.where": "chat",
                                                   "sluice.guard.type": f.type,
                                                   "sluice.guard.action": action})
                log.span.add_event("sluice.guard.findings", {
                    "sluice.guard.action": action,
                    "sluice.guard.findings": [json.dumps(f.brief()) for f in found]})
                if action == "block":
                    return _error(400, "The prompt contains what looks like a secret "
                                  f"({', '.join(sorted({f.type for f in found}))}); not sent",
                                  "secret_in_prompt")

        sh: SharedState = request.app.state.shared
        ex: Executor = request.app.state.executor
        messages = [Message(m.role, m.content) for m in req.messages]
        max_tokens = req.max_tokens or req.max_completion_tokens
        prompt_est = estimate_prompt_tokens(messages)
        est = prompt_est + (max_tokens or settings.limits.default_completion_estimate)
        tpm_bucket = f"{key.limit_id}:tpm"  # per lineage: a rotation shares its limits

        with tracer.start_as_current_span("sluice.limits", attributes={"sluice.tokens.estimate": est}):
            # --- 2. budget (checked before the call; in-flight requests can overshoot slightly)
            day, month = await sh.spend(key.limit_id)
            for spent, cap, label in ((day, key.daily_budget_usd, "daily"),
                                      (month, key.monthly_budget_usd, "monthly")):
                if cap is not None and spent >= cap * 1_000_000:
                    return _error(429, f"{label.capitalize()} budget of ${cap} exhausted",
                                  "budget_exceeded")

            # --- 3. rate limits: requests/min, then tokens/min (estimated, reconciled later) ---
            ok, retry = await sh.acquire(f"{key.limit_id}:rpm", key.rpm, key.rpm, 1)
            if not ok:
                return _rate_limited("requests", retry)
            if est > key.tpm:
                return _error(429, f"Request needs ~{est} tokens, above this key's {key.tpm}/min "
                              "limit", "rate_limit_exceeded")
            ok, retry = await sh.acquire(tpm_bucket, key.tpm, key.tpm, est)
            if not ok:
                return _rate_limited("tokens", retry)

        params = ChatParams(model="", messages=messages, temperature=req.temperature,
                            max_tokens=max_tokens or settings.limits.default_max_tokens,
                            response_format=req.response_format)
        cid = f"chatcmpl-{uuid.uuid4().hex[:24]}"
        created = int(time.time())

        # --- 4. semantic cache (after limits: a hit still counts as a request) ---------------
        with tracer.start_as_current_span("sluice.cache") as cs:
            if cache is None or not route.cache:
                probe = Probe("skip", "disabled")
            else:
                probe = await cache.probe(
                    tenant=key.team or f"key:{key.limit_id}", model=req.model, messages=messages,
                    temperature=req.temperature, max_tokens=params.max_tokens,
                    cache_control=request.headers.get("cache-control", ""))
            cs.set_attribute("sluice.cache.result", probe.result)
            if probe.reason:
                cs.set_attribute("sluice.cache.reason", probe.reason)
            if probe.match:
                cs.set_attribute("sluice.cache.similarity", round(probe.match.similarity, 4))
        log.cache = probe.result
        metrics.cache_lookups.add(1, {"sluice.cache.result": probe.result,
                                      "sluice.cache.reason": probe.reason or ""})
        if (ans := probe.answer) is not None:
            await sh.adjust(tpm_bucket, key.tpm, est)  # nothing went upstream: refund it all
            log.trail, log.finish_reason = Trail(provider="cache"), ans.finish_reason
            headers = {**_trail_headers(log.trail),
                       "x-sluice-cache-similarity": f"{probe.match.similarity:.4f}"}
            if not req.stream:
                return _completion(cid, created, req.model, ans.content, ans.finish_reason,
                                   ans.usage, headers)

            async def replay_done(r: StreamResult) -> None:
                log.outcome = r.outcome
                finish(log)

            return _stream_response(
                _sse(cid, created, req.model, Chunk(content=ans.content),
                     _one(Chunk(finish_reason=ans.finish_reason, usage=ans.usage)), replay_done),
                headers)

        def remember(content: str, finish_reason: str | None, usage: Usage) -> None:
            answer = CachedAnswer(messages[-1].content, content, finish_reason or "stop", usage)
            if cache and cache.put(probe, answer):
                log.span.add_event("sluice.cache.stored")

        # --- 5. do the work ----------------------------------------------------------------

        async def settle(bills: list[tuple[Target, Usage]]) -> None:
            """Reconcile the estimate with reality: fix the TPM bucket and record spend. One bill
            per upstream answer: none if all failed, two if an answer was escalated."""
            used = Usage(sum(u.prompt_tokens for _, u in bills),
                         sum(u.completion_tokens for _, u in bills))
            await sh.adjust(tpm_bucket, key.tpm, est - used.total_tokens)
            if bills:
                micro = sum(cost_micro_usd(u.prompt_tokens, u.completion_tokens,
                                           t.price_in_per_mtok, t.price_out_per_mtok)
                            for t, u in bills)
                log.usage, log.cost_micro = used, micro
                if micro:
                    await sh.add_spend(key.limit_id, micro)
                    await watch.spent(key, micro)  # R2 burn rate (ADR 0012)

        def billed(c: Completion) -> Usage:
            # upstream reported no usage: bill an estimate, not 0
            return c.usage if c.usage.total_tokens else Usage(prompt_est, len(c.content) // 4)

        async def escalate(c: Completion, trail: Trail, flags: list[str],
                           bills: list) -> tuple[Completion, Trail, bool]:
            """Re-ask on the stronger route. If that fails too, the cheap answer still stands."""
            log.span.add_event("sluice.escalate", {"sluice.checks": flags,
                                                   "sluice.provider": trail.provider or ""})
            for f in flags:
                metrics.escalations.add(1, {"sluice.check": f})
            try:
                c2, t2 = await ex.complete(route.escalate, params)
            except (AllProvidersFailed, NonRetryableError) as e:
                log.span.add_event("sluice.escalate_failed", {sc.ERROR_TYPE: type(e).__name__})
                return c, _merge(trail, e.trail), False
            bills.append((t2.target, billed(c2)))
            return c2, _merge(trail, t2), True

        try:
            if not req.stream:
                c, trail = await ex.complete(route, params)
                bills = [(trail.target, billed(c))]
                flags = [f for f in heuristic_flags(c.content, c.finish_reason, req.expects_json)
                         if f in route.escalate_on] if route.escalate else []
                escalated = False
                if flags:
                    c, trail, escalated = await escalate(c, trail, flags, bills)
                log.trail, log.finish_reason = trail, c.finish_reason
                await settle(bills)
                headers = _trail_headers(trail)
                if flags:
                    headers["x-sluice-escalated"] = ",".join(flags) + ("" if escalated
                                                                       else ";failed")
                if escalated or not flags:  # never cache an answer that failed a check
                    remember(c.content, c.finish_reason, bills[-1][1])
                return _completion(cid, created, req.model, c.content, c.finish_reason,
                                   bills[-1][1], headers)
            # Streaming: the executor fails over until the first chunk arrives. After that the
            # status line is sent and failure can only be signalled in-stream (ADR 0001). For
            # the same reason a streamed answer can't be checked and escalated: on an escalating
            # route, streams go straight to the stronger route.
            sroute = route.escalate or route
            (first, upstream), trail = await ex.stream(sroute, params)
            log.trail = trail
        except NonRetryableError as e:
            log.trail = e.trail
            await settle([])  # nothing was produced: refund the whole estimate
            return _error(e.error.status or 502, str(e.error), "upstream_rejected",
                          _trail_headers(e.trail))
        except AllProvidersFailed as e:
            log.trail = e.trail
            await settle([])
            return _upstream_error(e)

        def resumed(provider: str, chars: int, nxt: str) -> None:
            log.span.add_event("sluice.stream_resume", {
                "sluice.provider": provider, "sluice.next_provider": nxt,
                "sluice.resume.prefix_chars": chars, "sluice.resume.strategy": route.stream_resume})
            metrics.stream_resumes.add(1, {"sluice.provider": provider})

        rs = ResumableStream(ex, sroute, params, route.stream_resume, resumed, span=log.span)

        async def on_done(r: StreamResult) -> None:
            try:
                for s in rs.segments:
                    if s.cut:
                        ex.report_stream_failure(s.trail.provider)
                # no usage reported (error / disconnect): bill what we saw
                await settle(rs.bills(lambda s: Usage(prompt_est, len(s.text) // 4)))
                if r.outcome == "ok":  # only complete answers are worth replaying
                    remember(r.text, r.finish_reason, log.usage or Usage())
            finally:
                if len(rs.segments) > 1:
                    log.trail = functools.reduce(_merge, [s.trail for s in rs.segments])
                    log.span.set_attribute("sluice.resume.trimmed_chars", rs.trimmed)
                    if r.outcome == "ok":
                        r.outcome = "resumed"
                log.outcome = r.outcome
                log.finish_reason = r.finish_reason
                finish(log)

        return _stream_response(
            _sse(cid, created, req.model, None, rs.chunks(first, upstream, trail), on_done),
            _trail_headers(trail))

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
        if log.cache:
            span.set_attribute("sluice.cache.result", log.cache)
        if log.status >= 500 or log.outcome == "stream_interrupted":
            span.set_status(Status(StatusCode.ERROR, log.outcome))
        secs = time.monotonic() - log.t0
        metrics.requests.add(1, {sc.REQUEST_MODEL: log.model, "sluice.provider": provider,
                                 "sluice.outcome": log.outcome,
                                 "http.response.status_code": log.status})
        metrics.duration.record(secs, {sc.REQUEST_MODEL: log.model, "sluice.outcome": log.outcome})
        if ledger and trail and log.key:  # only requests that reached a provider cost anything
            ledger.record(  # buffered; written off the request path (ADR 0007)
                trace_id=format(span.get_span_context().trace_id, "032x"),
                key_id=log.key.id, key_name=log.key.name, team=log.key.team,
                app=log.key.app, model=log.model, provider=provider,
                upstream_model=upstream_model, status=log.status, outcome=log.outcome,
                attempts=len(trail.attempts), fallbacks=len(failovers),
                prompt_tokens=usage.prompt_tokens if usage else 0,
                completion_tokens=usage.completion_tokens if usage else 0,
                cost_micro_usd=log.cost_micro, latency_ms=round(secs * 1000, 1))
        span.end()

    async def _authenticate(request: Request) -> VirtualKey | None:
        auth = request.headers.get("authorization", "")
        # an empty "Bearer " is no key, not a probe (load balancers send it)
        bearer = (auth[7:].strip() or None) if auth.lower().startswith("bearer ") else None
        ip = client_ip(request.client.host if request.client else None,
                       request.headers.get("x-forwarded-for"), trusted_proxies)
        key = None
        if await watch.ip_blocked(ip):  # R4 (ADR 0012)
            why = "ip_blocked"
        elif bearer is None:
            why = "missing"
        elif not KEY_SHAPED.fullmatch(bearer):
            why = "malformed"  # quotes, spaces, SQL: never sent to the key lookup at all
        else:
            key, why = request.app.state.keys.check(bearer, ip)
        # The caller sees one 401 for every reason (ADR 0011); telemetry gets the real one
        trace.get_current_span().set_attribute("sluice.auth.result", why)
        if key is None:
            metrics.auth_rejections.add(1, {"sluice.auth.result": why})
            if why != "ip_blocked":
                await watch.auth_failed(ip, why, bearer)
        else:
            await watch.seen_from(key, ip)  # R3
        return key

    return app


async def _prune_audit(audit: McpAudit, days: float, every_s: float = 3600) -> None:
    """MCP audit retention (ADR 0012): hourly, off the event loop."""
    while True:
        await asyncio.to_thread(audit.prune, days)
        await asyncio.sleep(every_s)


def _frame(cid: str, created: int, model: str, delta: dict, finish: str | None) -> str:
    obj = {
        "id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    return f"data: {json.dumps(obj)}\n\n"


async def _sse(
    cid: str, created: int, model: str, first: Chunk | None, rest: AsyncIterator[Chunk],
    on_done: Callable[[StreamResult], Awaitable[None]],
) -> AsyncIterator[str]:
    usage: Usage | None = None
    text: list[str] = []
    finish_reason: str | None = None
    outcome = "client_disconnected"  # until the stream proves otherwise
    try:
        yield _frame(cid, created, model, {"role": "assistant", "content": ""}, None)
        async for chunk in _chain([first] if first else [], rest):
            if chunk.usage:
                usage = chunk.usage
            if chunk.finish_reason:
                finish_reason = chunk.finish_reason
            text.append(chunk.content)
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
            await on_done(StreamResult(usage, "".join(text), finish_reason, outcome))

        await asyncio.shield(finish())


async def _one(chunk: Chunk) -> AsyncIterator[Chunk]:
    yield chunk


def _completion(cid: str, created: int, model: str, content: str, finish_reason: str,
                usage: Usage, headers: dict[str, str]) -> JSONResponse:
    return JSONResponse(
        {
            "id": cid, "object": "chat.completion", "created": created, "model": model,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": finish_reason,
            }],
            "usage": {
                "prompt_tokens": usage.prompt_tokens,
                "completion_tokens": usage.completion_tokens,
                "total_tokens": usage.total_tokens,
            },
        },
        headers=headers,
    )


def _stream_response(body: AsyncIterator[str], headers: dict[str, str]) -> StreamingResponse:
    return StreamingResponse(body, media_type="text/event-stream", headers={
        "Cache-Control": "no-cache", "X-Accel-Buffering": "no", **headers})


async def _chain(head: list[Chunk], tail: AsyncIterator[Chunk]) -> AsyncIterator[Chunk]:
    for h in head:
        yield h
    async for t in tail:
        yield t


def _merge(first: Trail, second: Trail) -> Trail:
    """One trail for an escalated request: both chains' attempts, the answer's provider."""
    return Trail(first.attempts + second.attempts, first.fallbacks + second.fallbacks,
                 second.provider or first.provider, second.target or first.target)


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
