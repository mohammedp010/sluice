"""Chaos run: inject failures into the primary mock provider and measure what clients see.

Runs in-process (ASGI transport) against mock upstreams: free and reproducible.
    .venv/bin/python bench/chaos.py            # writes bench/results/chaos.json + chaos.md
"""
from __future__ import annotations

import asyncio
import json
import statistics
import time
from pathlib import Path

import httpx

from sluice.api.app import create_app
from sluice.config import Settings
from sluice.providers.mock import MockProvider

BODY = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
N, CONC = 200, 20
COOLDOWN = 1.0


def build():
    primary = MockProvider("primary", text="p", latency_ms=20, seed=1)
    backup = MockProvider("backup", text="b", latency_ms=20, seed=2)
    settings = Settings(
        providers={
            "primary": {"type": "mock", "breaker_failures": 3, "breaker_cooldown_s": COOLDOWN},
            "backup": {"type": "mock"},
        },
        routes={"m": {"deadline_s": 3, "chain": [
            {"provider": "primary", "upstream_model": "x", "retries": 1, "timeout_s": 0.25},
            {"provider": "backup", "upstream_model": "x", "retries": 0, "timeout_s": 0.25},
        ]}},
    )
    app = create_app(settings, {"primary": primary, "backup": backup})
    return app, primary


async def burst(client, n=N, stream=False):
    sem = asyncio.Semaphore(CONC)
    out = []

    async def one():
        async with sem:
            t0 = time.perf_counter()
            r = await client.post("/v1/chat/completions", json={**BODY, "stream": stream})
            ok = r.status_code == 200 and (not stream or "[DONE]" in r.text)
            out.append((ok, (time.perf_counter() - t0) * 1000, r.headers.get("x-sluice-provider")))

    await asyncio.gather(*[one() for _ in range(n)])
    lat = sorted(x[1] for x in out)
    return {
        "requests": n,
        "success_rate": round(sum(x[0] for x in out) / n, 4),
        "served_by_primary": sum(x[2] == "primary" for x in out),
        "served_by_backup": sum(x[2] == "backup" for x in out),
        "p50_ms": round(statistics.median(lat), 1),
        "p95_ms": round(lat[int(0.95 * n) - 1], 1),
    }


async def recovery_time(client, primary) -> float:
    """Primary dies (breaker opens), then heals. How long until it serves traffic again?"""
    primary.error_rate = 1.0
    await burst(client, 20)
    primary.error_rate = 0.0
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < 10:
        r = await client.post("/v1/chat/completions", json=BODY)
        if r.headers.get("x-sluice-provider") == "primary":
            return round(time.perf_counter() - t0, 2)
        await asyncio.sleep(0.05)
    return float("nan")


async def main():
    results = {}
    scenarios = {
        "baseline (healthy)": {},
        "primary down (500s)": {"error_rate": 1.0},
        "primary rate-limited (429s)": {"error_rate": 1.0, "error_status": 429},
        "primary flaky (30% 500s)": {"error_rate": 0.3},
        "primary slow (2s, timeout 0.25s)": {"latency_ms": 2000},
    }
    for name, knobs in scenarios.items():
        app, primary = build()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            for k, v in knobs.items():
                setattr(primary, k, v)
            results[name] = await burst(c)

    app, primary = build()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        results["_recovery"] = {"breaker_cooldown_s": COOLDOWN,
                                "seconds_until_primary_serves_again": await recovery_time(c, primary)}

    app, primary = build()
    primary.cut_stream_after = 1
    primary.text = "one two three four"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/v1/chat/completions", json={**BODY, "stream": True})
        results["_stream_cut_mid_stream"] = {
            "http_status": r.status_code,
            "got_done_marker": "[DONE]" in r.text,
            "got_error_event": "stream_interrupted" in r.text,
        }

    out = Path(__file__).parent / "results"
    out.mkdir(exist_ok=True)
    (out / "chaos.json").write_text(json.dumps(results, indent=2))
    rows = ["| scenario | success | primary | backup | p50 ms | p95 ms |", "|---|---|---|---|---|---|"]
    for k, v in results.items():
        if not k.startswith("_"):
            rows.append(f"| {k} | {v['success_rate']:.1%} | {v['served_by_primary']} | "
                        f"{v['served_by_backup']} | {v['p50_ms']} | {v['p95_ms']} |")
    md = ("# Chaos results\n\nMock upstreams, in-process, "
          f"{N} requests at concurrency {CONC} per scenario. Machine-specific; rerun to compare.\n\n"
          + "\n".join(rows) + "\n\n```json\n"
          + json.dumps({k: v for k, v in results.items() if k.startswith("_")}, indent=2) + "\n```\n")
    (out / "chaos.md").write_text(md)
    print(md)


if __name__ == "__main__":
    asyncio.run(main())
