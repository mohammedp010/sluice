"""Connections to the real MCP servers behind the gateway, via the official MCP SDK client.

Each server gets one long-lived task that opens the connection, keeps it open, and closes it.
The SDK's stdio and HTTP clients use anyio task groups, which must be entered and exited in
the *same* task. Opening a connection inside one request and closing it at shutdown (another
task) crashes, so neither does: requests only borrow the session.

Connections start lazily on first use, so the gateway boots even if a tool server is down; a
server that fails to start is retried on the next request after `retry_s`.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import time
from contextlib import AsyncExitStack
from datetime import timedelta

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolResult, Tool

from ..config import McpServerConfig
from .pins import ToolPins, definition

SEP = "__"  # public tool name = "<server>__<tool>"


class UpstreamDown(Exception):
    pass


class Upstreams:
    def __init__(self, servers: dict[str, McpServerConfig], tools_ttl_s: float = 60.0,
                 retry_s: float = 10.0, pins: ToolPins | None = None):
        self.cfg = servers
        self.pins = pins
        self.tools_ttl_s = tools_ttl_s
        self.retry_s = retry_s
        self._sessions: dict[str, ClientSession] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._failed_at: dict[str, float] = {}
        self._stop = asyncio.Event()
        self._lock = asyncio.Lock()
        self._tools: dict[str, list[Tool]] = {}
        self._tools_at: dict[str, float] = {}

    async def _hold(self, name: str, ready: asyncio.Future) -> None:
        cfg = self.cfg[name]
        try:
            async with AsyncExitStack() as stack:
                if cfg.command:
                    # Only the server's own `env:` block. The SDK adds a safe base (PATH, HOME,
                    # USER, ...) but never the rest of os.environ, which holds provider keys.
                    env = {k: os.path.expandvars(v) for k, v in cfg.env.items()}
                    params = StdioServerParameters(command=cfg.command[0], args=cfg.command[1:],
                                                   env=env)
                    read, write = await stack.enter_async_context(stdio_client(params))
                else:
                    http = await stack.enter_async_context(
                        httpx.AsyncClient(timeout=httpx.Timeout(cfg.timeout_s)))
                    read, write, _ = await stack.enter_async_context(
                        streamable_http_client(cfg.url, http_client=http))
                session = await stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                self._sessions[name] = session
                ready.set_result(None)
                await self._stop.wait()
        except Exception as e:  # noqa: BLE001 -- any startup failure means "server down"
            if not ready.done():
                ready.set_exception(UpstreamDown(f"{name}: {type(e).__name__}: {e}"))
        finally:
            self._sessions.pop(name, None)

    async def session(self, name: str) -> ClientSession:
        if name in self._sessions:
            return self._sessions[name]
        async with self._lock:
            if name in self._sessions:
                return self._sessions[name]
            if time.monotonic() - self._failed_at.get(name, -1e9) < self.retry_s:
                raise UpstreamDown(f"{name}: failed recently, retrying later")
            ready = asyncio.get_running_loop().create_future()
            self._tasks[name] = asyncio.create_task(self._hold(name, ready))
            try:
                await ready
            except UpstreamDown:
                self._failed_at[name] = time.monotonic()
                raise
            return self._sessions[name]

    async def tools(self) -> dict[str, Tool]:
        """Public name -> tool, from every server that is up. Cached for `tools_ttl_s`.
        Each refresh is checked against the pins (pins.py); filtering is the caller's job."""
        out: dict[str, Tool] = {}
        refreshed = []
        for name in self.cfg:
            if time.monotonic() - self._tools_at.get(name, -1e9) > self.tools_ttl_s:
                try:
                    s = await self.session(name)
                    self._tools[name] = (await s.list_tools()).tools
                    self._tools_at[name] = time.monotonic()
                    refreshed.append(name)
                except Exception:  # noqa: BLE001 -- one server down mustn't hide the others
                    self._tools.pop(name, None)
            for t in self._tools.get(name, []):
                out[f"{name}{SEP}{t.name}"] = t
        if self.pins:  # after the loop, so the lint knows every server's tool names
            for name in refreshed:
                others = {t.name for n, ts in self._tools.items() if n != name for t in ts}
                for t in self._tools[name]:
                    self.pins.observe(f"{name}{SEP}{t.name}", definition(t), others)
        return out

    async def call(self, public: str, args: dict) -> CallToolResult:
        server, _, tool = public.partition(SEP)
        s = await self.session(server)
        # the SDK's own timeout, not task cancellation: the lesson of ADR 0007
        return await s.call_tool(tool, args, read_timeout_seconds=timedelta(
            seconds=self.cfg[server].timeout_s))

    async def close(self) -> None:
        self._stop.set()
        for t in self._tasks.values():
            with contextlib.suppress(Exception):
                await asyncio.wait_for(t, 10)
