"""Open-loop load generator: requests leave on a fixed schedule, whether or not earlier ones
have come back.

    python bench/loadgen.py http://127.0.0.1:8100/v1/chat/completions --rps 100 --duration 15

Why open loop: a closed loop (N workers, each sending the next request when the last returns)
slows down exactly when the server does, so the requests that *would* have queued are never
sent and never measured. This is "coordinated omission", and it makes p99 look far better
than real users would see. Here latency is measured from each request's *scheduled* send time,
so queueing anywhere (server, network, or this generator) counts.

The generator reports its own lateness too: if it can't keep the schedule (laptop CPU
saturated), the numbers are labelled as suspect instead of silently blamed on the server.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics

import httpx

BODY = {"model": "bench", "messages": [{"role": "user", "content": "Say something short."}],
        "max_tokens": 32}


def pct(xs: list[float], p: float) -> float:
    if not xs:
        return float("nan")
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(p / 100 * len(xs)))]


async def run(url: str, rps: float, duration: float, warmup: float = 3.0,
              headers: dict | None = None, body: dict | None = None,
              timeout: float = 10.0) -> dict:
    loop = asyncio.get_running_loop()
    limits = httpx.Limits(max_connections=4000, max_keepalive_connections=4000)
    results: list[tuple[float, float, str, float]] = []  # (t_since_start, latency, status, lag)
    async with httpx.AsyncClient(limits=limits, timeout=timeout, headers=headers or {}) as client:

        async def one(t_sched: float, lag: float, t0: float) -> None:
            try:
                r = await client.post(url, json=body or BODY)
                status = str(r.status_code)
            except httpx.HTTPError as e:
                status = type(e).__name__
            results.append((t_sched - t0, loop.time() - t_sched, status, lag))

        n = int(rps * (warmup + duration))
        t0 = loop.time() + 0.1
        tasks = []
        for i in range(n):
            t = t0 + i / rps
            delay = t - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
            tasks.append(asyncio.create_task(one(t, loop.time() - t, t0)))
        await asyncio.gather(*tasks)

    kept = [r for r in results if r[0] >= warmup]  # warm-up: connections, JIT-free but caches
    ok = [lat for _, lat, s, _ in kept if s == "200"]
    statuses: dict[str, int] = {}
    for _, _, s, _ in kept:
        statuses[s] = statuses.get(s, 0) + 1
    lags = [lag for *_, lag in kept]
    return {
        "rps_target": rps, "duration_s": duration, "sent": len(kept),
        "ok": len(ok), "success_rate": len(ok) / len(kept) if kept else 0.0,
        "statuses": statuses,
        "p50_ms": pct(ok, 50) * 1000, "p95_ms": pct(ok, 95) * 1000, "p99_ms": pct(ok, 99) * 1000,
        "max_ms": max(ok) * 1000 if ok else float("nan"),
        "mean_ms": statistics.fmean(ok) * 1000 if ok else float("nan"),
        "gen_lag_p99_ms": pct(lags, 99) * 1000,  # > a few ms: the generator, not the server
    }


def new_loop_run(coro):
    """asyncio.run, on uvloop when installed: the generator must be faster than what it tests."""
    try:
        import uvloop
        return uvloop.run(coro)
    except ImportError:
        return asyncio.run(coro)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--rps", type=float, default=50)
    ap.add_argument("--duration", type=float, default=15)
    ap.add_argument("--warmup", type=float, default=3)
    ap.add_argument("--key", default=None, help="Bearer token")
    a = ap.parse_args()
    hdrs = {"Authorization": f"Bearer {a.key}"} if a.key else {}
    print(json.dumps(new_loop_run(run(a.url, a.rps, a.duration, a.warmup, hdrs)), indent=1))
