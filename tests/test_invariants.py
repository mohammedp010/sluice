"""Security invariants (ADR 0010). These fail when a change opens a door, not when it breaks a
feature: a new route nobody classified, a route that answers without its credential, or a
request field that could point the gateway somewhere else.

They come from the 2026 LiteLLM CVEs: an MCP "test connection" endpoint that ran whatever it was
sent (CVE-2026-42271), read-only users reaching admin functions (CVE-2026-35029), and a request
field that redirected upstream calls, provider key included (CVE-2026-59823).
"""
import httpx
import pytest
from conftest import ROUTE, make_app

from sluice.api.schemas import ChatCompletionRequest
from sluice.config import Settings

# Every route the app may serve, and who may call it. Adding a route means adding it here.
ROUTES = {
    ("/healthz", "GET"): "public",
    ("/v1/sluice/health", "GET"): "public",  # breaker states, for load balancers and the UI
    ("/metrics", "GET"): "public",  # Prometheus scrape, only with telemetry.prometheus
    ("/v1/models", "GET"): "key",
    ("/v1/chat/completions", "POST"): "key",
    ("/mcp", "POST"): "key",
    ("/mcp", "GET"): "refused",  # 405: no server-initiated stream
    ("/mcp", "DELETE"): "refused",
    **{(f"/v1/sluice/admin{p}", "GET"): "admin" for p in (
        "/overview", "/traces", "/traces/{trace_id}", "/ledger", "/ledger/summary", "/keys",
        "/mcp", "/security")},
}
# Request fields that would let a caller choose where, or as whom, the gateway calls out
STEERING = {"api_base", "base_url", "url", "api_key", "provider", "command", "args", "env",
            "headers", "extra_headers"}
BODY = {"model": "m", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 5}


def full_app():
    settings = Settings(providers={"mock": {"type": "mock"}}, routes={"m": ROUTE},
                        admin={"enabled": True})
    return make_app(settings)


def served(app) -> set[tuple[str, str]]:
    return {(r.path, m) for r in app.routes for m in getattr(r, "methods", None) or ()
            if m != "HEAD"}


def test_every_route_is_classified():
    app, _ = full_app()
    unknown = served(app) - ROUTES.keys()
    assert not unknown, f"classify these routes in ROUTES (and test them): {sorted(unknown)}"


async def test_each_route_demands_its_credential():
    app, _ = full_app()
    remote = httpx.ASGITransport(app=app, client=("203.0.113.7", 40000))  # not loopback
    async with httpx.AsyncClient(transport=remote, base_url="http://test") as c:
        for (path, method), cls in ROUTES.items():
            if (path, method) not in served(app):
                continue
            url = path.replace("{trace_id}", "0" * 32)
            r = await c.request(method, url, json=BODY if method == "POST" else None)
            want = {"public": 200, "key": 401, "admin": 403, "refused": 405}[cls]
            assert r.status_code == want, f"{method} {path} with no credential: {r.status_code}"


def test_no_request_field_can_steer_the_upstream():
    assert not STEERING & ChatCompletionRequest.model_fields.keys()
    # and unknown fields are dropped, not passed through to the provider call
    assert ChatCompletionRequest.model_config["extra"] == "ignore"
    req = ChatCompletionRequest.model_validate({**BODY, "api_base": "http://evil.example"})
    assert "api_base" not in req.model_dump()


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
async def test_no_api_map_for_strangers(path):
    app, _ = full_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as c:
        assert (await c.get(path)).status_code == 404
