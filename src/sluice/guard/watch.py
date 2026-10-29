"""Behaviour watch (ADR 0012): notice a valid key doing invalid things, and act at machine speed.

The 2026 intrusions ran start to finish in 12-20 minutes. A human reading an alert doesn't win
that race, so the rules act on their own. Every automatic action can be undone: suspend, never
revoke, and IP blocks expire.

  R1 probe     refused tool calls (denied, unknown, pending, quarantined) pile up -> suspend key
  R2 burn      a large share of the daily budget goes in minutes (LLMjacking)      -> suspend key
  R3 network   first request from a network this key hasn't used lately          -> alert
  R4 scanner   many failed key checks from one IP, or a bearer value that isn't
               key-shaped (the LiteLLM SQLi probes put quotes and UNION in it)   -> block the IP
  R5 egress    the egress guard blocked a tool call with a secret in it          -> suspend key

Counts live in the shared state (Redis in production), so replicas agree. Alerts go to a span
event, a JSON log line on the `sluice.security` logger, the `sluice.security.events` counter, an
in-memory list for the admin API, and optionally a Slack-compatible webhook.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import re
import time
from collections import deque

import httpx
from opentelemetry import trace

from ..auth.keys import KeyStore, VirtualKey
from ..config import WatchConfig

log = logging.getLogger("sluice.security")
KEY_SHAPED = re.compile(r"[A-Za-z0-9_.~+/=-]{1,512}")  # anything else in a bearer is a probe
DAY = 86400


def network(ip: str) -> str:
    """The /24 (IPv6: /64) an address belongs to: one office or one cloud subnet, roughly."""
    addr = ipaddress.ip_address(ip)
    return str(ipaddress.ip_network(f"{ip}/{24 if addr.version == 4 else 64}", strict=False))


class Alerts:
    def __init__(self, webhook_url: str | None, metrics=None, keep: int = 200):
        self.webhook_url = webhook_url
        self.metrics = metrics
        self.recent: deque[dict] = deque(maxlen=keep)
        self.transport: httpx.AsyncBaseTransport | None = None  # tests swap in a mock
        self._tasks: set[asyncio.Task] = set()

    def emit(self, rule: str, action: str, **attrs) -> dict:
        event = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "rule": rule,
                 "action": action, **attrs}
        self.recent.appendleft(event)
        trace.get_current_span().add_event(
            f"sluice.security.{rule}", {k: str(v) for k, v in event.items()})
        log.warning(json.dumps(event))
        if self.metrics:
            self.metrics.security_events.add(1, {"sluice.security.rule": rule,
                                                 "sluice.security.action": action})
        if self.webhook_url:  # fire and forget; an alert must never slow or fail a request
            t = asyncio.get_running_loop().create_task(self._post(event))
            self._tasks.add(t)
            t.add_done_callback(self._tasks.discard)
        return event

    async def _post(self, event: dict) -> None:
        text = f"Sluice {event['rule']}: {event['action']} " + ", ".join(
            f"{k}={v}" for k, v in event.items() if k not in ("ts", "rule", "action"))
        try:
            async with httpx.AsyncClient(timeout=3, transport=self.transport) as c:
                await c.post(self.webhook_url, json={"text": text, "sluice": event})
        except httpx.HTTPError as e:
            log.warning("alert webhook failed: %s", type(e).__name__)


class Watch:
    def __init__(self, cfg: WatchConfig, state, keys: KeyStore | None, metrics=None):
        self.cfg = cfg
        self.state = state
        self.keys = keys
        self.alerts = Alerts(os.environ.get(cfg.webhook_env) or None, metrics, cfg.recent_alerts)
        self._seen: dict[str, float] = {}  # local cache for R3, so most requests skip the store

    # --- R4 -------------------------------------------------------------------------------
    async def ip_blocked(self, ip: str | None) -> bool:
        return bool(ip) and self.cfg.enabled and await self.state.marked(f"ipblock:{ip}")

    async def auth_failed(self, ip: str | None, reason: str, bearer: str | None) -> None:
        if not self.cfg.enabled or not ip or reason == "missing":
            return
        probe = bearer is not None and not KEY_SHAPED.fullmatch(bearer)
        n = await self.state.count(f"authfail:{ip}", 1, self.cfg.scanner_window_s)
        if probe or n >= self.cfg.scanner_failures:
            why = "bearer value is not key-shaped" if probe else f"{n} failed key checks"
            if not self.cfg.enforce:
                self.alerts.emit("R4_scanner", "alert", ip=ip, why=why)
            elif await self.state.mark(f"ipblock:{ip}", self.cfg.block_ip_s):
                self.alerts.emit("R4_scanner", "block_ip", ip=ip, why=why,
                                 seconds=self.cfg.block_ip_s)

    # --- R3 -------------------------------------------------------------------------------
    async def seen_from(self, key: VirtualKey, ip: str | None) -> None:
        if not self.cfg.enabled or not ip or key.id == 0:
            return
        try:
            net = network(ip)
        except ValueError:
            return
        k = f"net:{key.limit_id}:{net}"
        now = time.monotonic()
        if self._seen.get(k, 0) > now:
            return
        self._seen[k] = now + 3600  # ask the store again in an hour at most
        if len(self._seen) > 100_000:
            self._seen.clear()
        if await self.state.mark(k, self.cfg.new_network_days * DAY):
            first = await self.state.mark(f"net:{key.limit_id}:any", 3650 * DAY)
            self.alerts.emit("R3_new_network", "alert", key_id=key.id, key=key.name,
                             team=key.team, network=net, first_use=first)

    # --- R1, R2, R5 -------------------------------------------------------------------------
    async def tool_refused(self, key: VirtualKey, outcome: str) -> None:
        if not self.cfg.enabled or key.id == 0:
            return
        n = await self.state.count(f"probe:{key.id}", 1, self.cfg.probe_window_s)
        if n >= self.cfg.probe_calls:
            await self._suspend(key, "R1_probe", f"{n} refused tool calls in "
                                f"{self.cfg.probe_window_s}s (last: {outcome})")

    async def spent(self, key: VirtualKey, micro_usd: int) -> None:
        if not self.cfg.enabled or key.id == 0 or micro_usd <= 0:
            return
        cap = (key.daily_budget_usd * self.cfg.burn_fraction if key.daily_budget_usd
               else self.cfg.burn_usd)
        if cap is None:
            return
        n = await self.state.count(f"burn:{key.limit_id}", micro_usd, self.cfg.burn_window_s)
        if n >= cap * 1_000_000:
            await self._suspend(key, "R2_burn_rate", f"${n / 1e6:.4f} in "
                                f"{self.cfg.burn_window_s}s (limit ${cap:.4f})")

    async def egress_blocked(self, key: VirtualKey, tool: str, types: list[str]) -> None:
        if self.cfg.enabled and key.id != 0:
            await self._suspend(key, "R5_egress", f"secret in arguments to {tool}: "
                                + ", ".join(types))

    async def _suspend(self, key: VirtualKey, rule: str, why: str) -> None:
        attrs = {"key_id": key.id, "key": key.name, "team": key.team, "why": why}
        if not self.cfg.enforce or self.keys is None:
            if await self.state.mark(f"alerted:{rule}:{key.id}", 3600):  # once an hour
                self.alerts.emit(rule, "alert", **attrs)
            return
        if await asyncio.to_thread(self.keys.suspend, key.id, f"auto:{rule}: {why}"[:200]):
            self.alerts.emit(rule, "suspend", **attrs)
