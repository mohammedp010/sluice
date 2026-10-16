"""Burst test: several keys hammer TWO gateway instances that share one Redis.

Expect each key to get exactly its bucket capacity of 200s in a fast burst (refill during
the burst is negligible), and no key to affect another.
    SLUICE_TEST_REDIS=redis://localhost:6399/15 .venv/bin/python bench/ratelimit.py
"""
import asyncio
import json
import os
import sys
from pathlib import Path

import redis

sys.path.insert(0, str(Path(__file__).parent.parent / "tests"))
from conftest import ROUTE, client_for

from sluice.api.app import create_app
from sluice.auth.keys import KeyStore
from sluice.config import Settings
from sluice.limits.state import RedisState
from sluice.providers.mock import MockProvider

URL = os.environ.get("SLUICE_TEST_REDIS", "redis://localhost:6399/15")
BODY = {"model": "m", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 3}


async def main():
    redis.Redis.from_url(URL).flushdb()
    keys = KeyStore(":memory:")
    limits = {"alpha": 30, "beta": 60, "gamma": 10}
    raws = {n: keys.create(n, rpm=r)[0] for n, r in limits.items()}
    settings = Settings(providers={"mock": {"type": "mock"}}, routes={"m": ROUTE},
                        telemetry={"ledger": False})
    apps = [create_app(settings, {"mock": MockProvider()}, state=RedisState(URL), keystore=keys)
            for _ in range(2)]
    clients = {n: [client_for(a, raw) for a in apps] for n, raw in raws.items()}

    async def fire(name, i):
        return name, (await clients[name][i % 2].post("/v1/chat/completions", json=BODY)).status_code

    jobs = [fire(n, i) for n in limits for i in range(100)]
    res = await asyncio.gather(*jobs)
    out = {}
    for n, limit in limits.items():
        codes = [c for k, c in res if k == n]
        out[n] = {"rpm_limit": limit, "sent": 100, "200": codes.count(200), "429": codes.count(429),
                  "exact": codes.count(200) == limit}
    p = Path(__file__).parent / "results" / "ratelimit.json"
    p.write_text(json.dumps(out, indent=2))
    print(json.dumps(out, indent=2))


asyncio.run(main())
