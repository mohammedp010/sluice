"""Shared mutable state for limits, budgets and the paid-call guard.

Two implementations of one interface:
  MemoryState  single process; also the readable reference for what the Lua script does.
  RedisState   shared across gateway instances; every read-modify-write is one atomic Lua call.

Rate limiting is a token bucket: capacity C tokens, refilling at C/60 per second. A request
costs tokens; if the bucket can't cover it, we say how long until it can. RPM and TPM are just
two buckets per key with different costs (1 per request vs. estimated tokens).
"""
from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol

import redis.asyncio as aioredis

# (allowed, retry_after_seconds)
Acquire = tuple[bool, float]


class SharedState(Protocol):
    async def acquire(self, bucket: str, capacity: float, per_minute: float, cost: float) -> Acquire: ...
    async def adjust(self, bucket: str, capacity: float, delta: float) -> None: ...
    async def spend(self, key_id: int) -> tuple[int, int]: ...  # (today, this month) micro-USD
    async def add_spend(self, key_id: int, micro_usd: int) -> None: ...
    async def incr_paid_calls(self) -> int: ...  # today's count, after incrementing
    # Behaviour watch (ADR 0012). A fixed window: the count resets `window_s` after its first
    # event, so a burst across a boundary can split in two. Good enough for thresholds.
    async def count(self, name: str, by: int, window_s: int) -> int: ...
    async def mark(self, name: str, ttl_s: int) -> bool: ...  # True if it wasn't set already
    async def marked(self, name: str) -> bool: ...
    async def aclose(self) -> None: ...


def _periods(now: float | None = None) -> tuple[str, str]:
    d = datetime.fromtimestamp(now if now is not None else time.time(), tz=UTC)
    return d.strftime("%Y%m%d"), d.strftime("%Y%m")


class MemoryState:
    def __init__(self, clock: Callable[[], float] = time.time):
        self._clock = clock
        self._buckets: dict[str, tuple[float, float]] = {}  # name -> (tokens, last_ts)
        self._spend: dict[str, int] = {}
        self._paid: dict[str, int] = {}
        self._windows: dict[str, tuple[int, float]] = {}  # name -> (value, expires_at)

    async def acquire(self, bucket, capacity, per_minute, cost) -> Acquire:
        rate = per_minute / 60
        now = self._clock()
        tokens, ts = self._buckets.get(bucket, (capacity, now))
        tokens = min(capacity, tokens + (now - ts) * rate)
        if tokens >= cost:
            self._buckets[bucket] = (tokens - cost, now)
            return True, 0.0
        self._buckets[bucket] = (tokens, now)
        return False, (cost - tokens) / rate

    async def adjust(self, bucket, capacity, delta) -> None:
        if bucket in self._buckets:  # same as the Lua script: nothing to adjust if it never existed
            tokens, ts = self._buckets[bucket]
            self._buckets[bucket] = (min(capacity, tokens + delta), ts)

    async def spend(self, key_id):
        d, m = _periods(self._clock())
        return self._spend.get(f"{key_id}:d:{d}", 0), self._spend.get(f"{key_id}:m:{m}", 0)

    async def add_spend(self, key_id, micro_usd):
        d, m = _periods(self._clock())
        for k in (f"{key_id}:d:{d}", f"{key_id}:m:{m}"):
            self._spend[k] = self._spend.get(k, 0) + micro_usd

    async def incr_paid_calls(self):
        d, _ = _periods(self._clock())
        self._paid[d] = self._paid.get(d, 0) + 1
        return self._paid[d]

    def _live(self, name: str) -> tuple[int, float] | None:
        v = self._windows.get(name)
        if v is not None and v[1] <= self._clock():
            del self._windows[name]
            return None
        return v

    async def count(self, name, by, window_s):
        value, exp = self._live(name) or (0, self._clock() + window_s)
        self._windows[name] = (value + by, exp)
        return value + by

    async def mark(self, name, ttl_s):
        if self._live(name):
            return False
        self._windows[name] = (1, self._clock() + ttl_s)
        return True

    async def marked(self, name):
        return self._live(name) is not None

    async def aclose(self):
        return None


# KEYS[1]=bucket  ARGV: capacity, refill_per_sec, cost.  Uses Redis' own clock (TIME) so every
# gateway instance agrees on "now" regardless of local clock skew.
_ACQUIRE = """
local cap  = tonumber(ARGV[1])
local rate = tonumber(ARGV[2])
local cost = tonumber(ARGV[3])
local t    = redis.call('TIME')
local now  = tonumber(t[1]) + tonumber(t[2]) / 1e6
local d      = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local tokens = tonumber(d[1])
local ts     = tonumber(d[2])
if tokens == nil then tokens = cap; ts = now end
tokens = math.min(cap, tokens + (now - ts) * rate)
local allowed, retry = 0, 0
if tokens >= cost then tokens = tokens - cost; allowed = 1 else retry = (cost - tokens) / rate end
redis.call('HSET', KEYS[1], 'tokens', tokens, 'ts', now)
redis.call('EXPIRE', KEYS[1], math.ceil(cap / rate) * 2 + 1)
return {allowed, tostring(retry)}
"""

# Refund (delta > 0) or charge (delta < 0) after the real usage is known. May go negative: debt.
_ADJUST = """
local cap = tonumber(ARGV[1])
local delta = tonumber(ARGV[2])
local d = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local tokens = tonumber(d[1])
if tokens == nil then return 0 end
redis.call('HSET', KEYS[1], 'tokens', math.min(cap, tokens + delta))
return 1
"""


class RedisState:
    PREFIX = "sluice:"

    def __init__(self, url: str, max_connections: int = 64):
        # Blocking pool: bursts beyond max_connections wait for a free connection instead of
        # erroring ("Too many connections" showed up in the first 300-request burst test).
        pool = aioredis.BlockingConnectionPool.from_url(
            url, decode_responses=True, max_connections=max_connections, timeout=5)
        self._r = aioredis.Redis(connection_pool=pool)
        self._acquire = self._r.register_script(_ACQUIRE)
        self._adjust = self._r.register_script(_ADJUST)

    async def acquire(self, bucket, capacity, per_minute, cost) -> Acquire:
        ok, retry = await self._acquire(
            keys=[self.PREFIX + "bucket:" + bucket], args=[capacity, per_minute / 60, cost])
        return bool(ok), float(retry)

    async def adjust(self, bucket, capacity, delta) -> None:
        await self._adjust(keys=[self.PREFIX + "bucket:" + bucket], args=[capacity, delta])

    async def spend(self, key_id):
        d, m = _periods()
        day, month = await self._r.mget(
            f"{self.PREFIX}spend:{key_id}:d:{d}", f"{self.PREFIX}spend:{key_id}:m:{m}")
        return int(day or 0), int(month or 0)

    async def add_spend(self, key_id, micro_usd):
        d, m = _periods()
        async with self._r.pipeline(transaction=True) as p:
            dk, mk = f"{self.PREFIX}spend:{key_id}:d:{d}", f"{self.PREFIX}spend:{key_id}:m:{m}"
            p.incrby(dk, micro_usd).expire(dk, 2 * 86400)
            p.incrby(mk, micro_usd).expire(mk, 32 * 86400)
            await p.execute()

    async def incr_paid_calls(self):
        d, _ = _periods()
        k = f"{self.PREFIX}paid_calls:{d}"
        n = await self._r.incr(k)
        await self._r.expire(k, 2 * 86400)
        return int(n)

    async def count(self, name, by, window_s):
        k = f"{self.PREFIX}win:{name}"
        async with self._r.pipeline(transaction=True) as p:
            p.incrby(k, by).expire(k, window_s, nx=True)  # the window starts at the first event
            n, _ = await p.execute()
        return int(n)

    async def mark(self, name, ttl_s):
        return bool(await self._r.set(f"{self.PREFIX}win:{name}", 1, ex=ttl_s, nx=True))

    async def marked(self, name):
        return bool(await self._r.exists(f"{self.PREFIX}win:{name}"))

    async def aclose(self):
        await self._r.aclose()
