"""MCP endpoint, written by hand: JSON-RPC 2.0 over Streamable HTTP, stateless.

A client POSTs one JSON-RPC message to /mcp and gets one JSON response back (the spec allows
plain JSON instead of an SSE stream). Notifications get 202 and no body. There is no session id
and no server-initiated stream (GET answers 405), which the spec permits and which keeps every
request independent, so any replica can serve it.

Supported: initialize, ping, tools/list, tools/call. Tools from every upstream server are
merged under "<server>__<tool>" names and filtered by the caller's key (deny by default). A
tool the key may not use is reported exactly like one that doesn't exist, so a key can't probe
for what's there. Every tools/call is audited, denials included.

Tool pinning (pins.py, ADR 0011): a tool whose definition changed since it was trusted
(quarantined), or that hasn't been trusted yet (pending), is hidden and refused the same way.

Egress guard (guard/secrets.py, ADR 0012): arguments are scanned for secrets before they are
forwarded (audit or block, per tool glob), and results before they are returned (audit or
redact). The audit row records each finding's type and position, never the value.
"""
from __future__ import annotations

import json
import time

from opentelemetry import trace

from ..auth.keys import VirtualKey
from ..config import GuardConfig
from ..guard.secrets import Finding, redact_json, scan_json
from .audit import McpAudit, args_digest
from .upstream import UpstreamDown, Upstreams

SUPPORTED = ["2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"]  # newest first
PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS, INTERNAL = (
    -32700, -32600, -32601, -32602, -32603)


def rpc_error(id_, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


def _result(id_, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": id_, "result": result}


class McpProxy:
    def __init__(self, upstreams: Upstreams, audit: McpAudit, tracer,
                 guard: GuardConfig | None = None, metrics=None):
        self.up = upstreams
        self.audit = audit
        self.tracer = tracer
        self.guard = guard or GuardConfig()
        self.metrics = metrics

    def _seen(self, span, where: str, action: str, found: list[Finding]) -> list[dict]:
        briefs = [{"where": where, **f.brief()} for f in found]
        span.add_event("sluice.guard.findings", {
            "sluice.guard.where": where, "sluice.guard.action": action,
            "sluice.guard.findings": [json.dumps(b) for b in briefs]})
        if self.metrics:
            for f in found:
                self.metrics.guard_findings.add(1, {"sluice.guard.where": where,
                                                    "sluice.guard.type": f.type,
                                                    "sluice.guard.action": action})
        return briefs

    async def handle(self, msg: object, key: VirtualKey) -> dict | None:
        """One JSON-RPC message in, one response out (None for notifications)."""
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or "method" not in msg:
            return rpc_error(msg.get("id") if isinstance(msg, dict) else None, INVALID_REQUEST,
                          "not a JSON-RPC 2.0 request")
        id_, method, params = msg.get("id"), msg["method"], msg.get("params") or {}
        if "id" not in msg:
            return None  # notification (e.g. notifications/initialized): nothing to answer
        if method == "initialize":
            asked = params.get("protocolVersion")
            return _result(id_, {
                "protocolVersion": asked if asked in SUPPORTED else SUPPORTED[0],
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "sluice", "version": "0.1.0"},
                "instructions": "Tools from several MCP servers, filtered by your key.",
            })
        if method == "ping":
            return _result(id_, {})
        if method == "tools/list":
            tools = await self.up.tools()
            return _result(id_, {"tools": [
                {**t.model_dump(exclude_none=True, by_alias=True), "name": name}
                for name, t in sorted(tools.items())
                if key.allows_tool(name) and self._trusted(name)]})
        if method == "tools/call":
            return await self._call(id_, params, key)
        return rpc_error(id_, METHOD_NOT_FOUND, f"method not supported: {method}")

    def _trusted(self, name: str) -> bool:
        return self.up.pins is None or self.up.pins.status(name) == "approved"

    async def _call(self, id_, params: dict, key: VirtualKey) -> dict:
        name, args = params.get("name"), params.get("arguments") or {}
        if not isinstance(name, str) or not isinstance(args, dict):
            return rpc_error(id_, INVALID_PARAMS, "tools/call needs a name and an arguments object")
        digest, size = args_digest(args)
        with self.tracer.start_as_current_span(
            f"execute_tool {name}", kind=trace.SpanKind.CLIENT,
            attributes={"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": name,
                        "sluice.key.id": key.id},
        ) as span:
            tid = format(span.get_span_context().trace_id, "032x")
            audit = {"trace_id": tid, "key_id": key.id, "key_name": key.name, "team": key.team,
                     "tool": name, "args_sha256": digest, "args_bytes": size}
            t0 = time.monotonic()
            egress: list[dict] = []

            async def done(outcome: str, error: str | None = None) -> None:
                span.set_attribute("sluice.outcome", outcome)
                await self.audit.record(**audit, outcome=outcome, error=error,
                                        latency_ms=round((time.monotonic() - t0) * 1000, 1),
                                        egress=json.dumps(egress) if egress else None)

            known = await self.up.tools()
            if not key.allows_tool(name) or name not in known or not self._trusted(name):
                outcome = ("unknown" if name not in known else "denied"
                           if not key.allows_tool(name) else self.up.pins.status(name))
                await done(outcome)  # denied, unknown, pending or quarantined
                return rpc_error(id_, INVALID_PARAMS, f"Unknown tool: {name}")  # same for all
            mode = GuardConfig.mode(self.guard.tool_args, name)
            found = scan_json(args) if mode != "off" else []
            if found:
                egress += self._seen(span, "tool_args", mode, found)
                if mode == "block":
                    await done("egress_blocked")
                    kinds = ", ".join(sorted({f.type for f in found}))
                    return rpc_error(id_, INVALID_PARAMS,
                                     f"Tool call blocked: the arguments contain a secret ({kinds})")
            try:
                result = await self.up.call(name, args)
            except UpstreamDown as e:
                await done("upstream_down", str(e))
                return rpc_error(id_, INTERNAL, f"tool server unavailable: {name}")
            except Exception as e:  # noqa: BLE001 -- timeouts, protocol errors from the server
                await done("error", f"{type(e).__name__}: {e}"[:300])
                return rpc_error(id_, INTERNAL, f"tool call failed: {type(e).__name__}")
            out = result.model_dump(exclude_none=True, by_alias=True)
            mode = GuardConfig.mode(self.guard.tool_results, name)
            found = scan_json(out) if mode != "off" else []
            if found:
                egress += self._seen(span, "tool_results", mode, found)
                if mode == "redact":
                    out = redact_json(out)
            await done("tool_error" if result.isError else "ok")
            return _result(id_, out)
