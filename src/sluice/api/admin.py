"""Read-only admin API for the UI (`UI/`). Enabled by `admin.enabled` in the policy.

Everything here reads state the gateway already keeps: the loaded policy, breaker states, the
recent traces (telemetry/recent.py), the usage ledger, the key store and the MCP audit log.
Nothing writes, and nothing changes how a request is handled. Key hashes are never returned,
only the 4-character hint the admin CLI shows.

Access (ADR 0010): with no admin token set, only loopback clients get in. With one set (the env
var named by `admin.token_env`), every request needs it as a bearer token. A virtual key never
opens the admin API: it is a separate credential, so a leaked app key can't read the ledger or
the key list. It is still off by default and off in the docker-compose policy.
"""
from __future__ import annotations

import hmac
import ipaddress
import os

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from ..auth.keys import client_ip
from ..config import Route
from ..telemetry.ledger import GROUPS
from ..telemetry.recent import RecentTraces

SUMMARY_FIELDS = ("group", "requests", "errors", "fallbacks", "prompt_tokens",
                  "completion_tokens", "cost_usd")


def _route(r: Route) -> dict:
    d = r.model_dump(exclude={"escalate"})
    d["escalate"] = _route(r.escalate) if r.escalate else None
    return d


MIN_TOKEN_CHARS = 32


def _is_loopback(host: str | None) -> bool:
    try:
        return ipaddress.ip_address(host or "").is_loopback
    except ValueError:  # no client, or not an IP
        return False


def admin_guard(token_env: str, trusted_proxies: list | None = None):
    """A dependency that admits a request to the admin API, or raises 401/403."""
    token = os.environ.get(token_env) or None  # read once, like provider keys
    if token is not None and len(token) < MIN_TOKEN_CHARS:
        raise ValueError(f"{token_env} must be at least {MIN_TOKEN_CHARS} characters")

    async def check(request: Request) -> None:
        if token is None:
            # behind a trusted proxy on this host, the peer is loopback but the caller isn't
            ip = client_ip(request.client.host if request.client else None,
                           request.headers.get("x-forwarded-for"), trusted_proxies or [])
            if not _is_loopback(ip):
                raise HTTPException(403, f"admin API is loopback-only until {token_env} is set")
            return
        auth = request.headers.get("authorization", "")
        given = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        if not hmac.compare_digest(given.encode(), token.encode()):  # constant time
            raise HTTPException(401, "invalid or missing admin token",
                                headers={"WWW-Authenticate": "Bearer"})

    return check


def admin_router(recent: RecentTraces, token_env: str,
                 trusted_proxies: list | None = None) -> APIRouter:
    api = APIRouter(prefix="/v1/sluice/admin",
                    dependencies=[Depends(admin_guard(token_env, trusted_proxies))])

    @api.get("/overview")
    async def overview(request: Request):
        st = request.app.state
        s = st.settings
        breakers = st.executor.breakers
        providers = {}
        for name, c in s.providers.items():
            b = breakers.get(name)
            providers[name] = {
                **c.model_dump(exclude={"api_key_env"}),
                "has_api_key": bool(c.api_key_env and os.environ.get(c.api_key_env)),
                "breaker": {"state": b.state.value, "failures": b.failures,
                            "threshold": b.failure_threshold, "cooldown_s": b.cooldown_s}
                if b else None,
            }
        return {
            "routes": {m: _route(r) for m, r in s.routes.items()},
            "providers": providers,
            "auth": {"enabled": s.auth.enabled, "max_body_bytes": s.auth.max_body_bytes},
            "state": {"backend": s.state.backend},
            "limits": s.limits.model_dump(),
            "paid_allowed": os.environ.get("SLUICE_ALLOW_PAID") == "1",
            "cache": s.cache.model_dump(exclude={"embed_base_url"}),
            "telemetry": s.telemetry.model_dump(exclude={"otlp_endpoint"}),
            "mcp": {"servers": {n: ("stdio" if m.command else "http")
                                for n, m in s.mcp.servers.items()},
                    "allowed_origins": s.mcp.allowed_origins},
            "recent_traces": recent.max_traces,
        }

    @api.get("/traces")
    async def traces(limit: int = Query(50, ge=1, le=500)):
        return recent.roots(limit)

    @api.get("/traces/{trace_id}")
    async def trace(trace_id: str, request: Request):
        spans = recent.get(trace_id)
        ledger = request.app.state.ledger
        row = ledger.by_trace(trace_id) if ledger else None
        if spans is None and row is None:
            raise HTTPException(404, "trace not held (only the most recent are kept in memory)")
        return {"trace_id": trace_id, "spans": spans or [], "ledger": row}

    @api.get("/ledger")
    async def ledger_rows(request: Request, limit: int = Query(100, ge=1, le=1000)):
        ledger = request.app.state.ledger
        return ledger.recent(limit) if ledger else []

    @api.get("/ledger/summary")
    async def ledger_summary(request: Request, by: str = "team", since: str | None = None):
        if by not in GROUPS:
            raise HTTPException(400, f"by must be one of {sorted(GROUPS)}")
        ledger = request.app.state.ledger
        rows = ledger.summary(by, since) if ledger else []
        return [dict(zip(SUMMARY_FIELDS, r)) for r in rows]

    @api.get("/keys")
    async def keys(request: Request):
        st = request.app.state
        if st.keys is None:
            return []
        out = []
        for k, hint in st.keys.list():
            day, month = await st.shared.spend(k.limit_id)  # shared by a rotation
            out.append({
                "id": k.id, "name": k.name, "team": k.team, "app": k.app, "hint": hint,
                "allowed_models": k.allowed_models, "allowed_tools": k.allowed_tools,
                "rpm": k.rpm, "tpm": k.tpm, "daily_budget_usd": k.daily_budget_usd,
                "monthly_budget_usd": k.monthly_budget_usd, "revoked": k.revoked,
                "status": k.status, "suspended_reason": k.suspended_reason,
                "expires_at": k.expires_at, "allowed_cidrs": k.allowed_cidrs,
                "lineage": k.limit_id, "created_at": k.created_at,
                "spend_today_usd": day / 1e6, "spend_month_usd": month / 1e6,
            })
        return out

    @api.get("/security")
    async def security(request: Request):
        """The behaviour watch's recent alerts and actions (newest first), the lockdown flag, and the
        rules in force."""
        st = request.app.state
        return {"alerts": list(st.watch.alerts.recent),
                "paid_off": bool(st.keys and st.keys.flag("paid_off")),
                "enforce": st.settings.watch.enforce and st.settings.watch.enabled,
                "watch": st.settings.watch.model_dump(exclude={"webhook_env"}),
                "guard": st.settings.guard.model_dump()}

    @api.get("/mcp")
    async def mcp(request: Request, limit: int = Query(100, ge=1, le=1000)):
        audit = request.app.state.mcp.audit
        return {
            "servers": {n: ("stdio" if m.command else "http")
                        for n, m in request.app.state.settings.mcp.servers.items()},
            "summary": [dict(zip(("key", "tool", "outcome", "calls"), r))
                        for r in audit.summary()],
            "audit": audit.rows(limit),
            "pins": request.app.state.mcp.up.pins.rows() if request.app.state.mcp.up.pins else [],
        }

    return api
