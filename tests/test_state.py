"""Same behaviour tests run against MemoryState and RedisState (parity)."""
import os

import pytest
import redis

from sluice.limits.state import MemoryState, RedisState

REDIS_URL = os.environ.get("SLUICE_TEST_REDIS", "redis://localhost:6399/15")


def redis_available() -> bool:
    try:
        redis.Redis.from_url(REDIS_URL).ping()
        return True
    except redis.RedisError:
        return False


@pytest.fixture(params=["memory", "redis"])
async def state(request):
    if request.param == "memory":
        yield MemoryState()
        return
    if not redis_available():
        pytest.skip("no redis at " + REDIS_URL)
    redis.Redis.from_url(REDIS_URL).flushdb()
    s = RedisState(REDIS_URL)
    yield s
    await s.aclose()


async def test_burst_up_to_capacity_then_reject(state):
    results = [(await state.acquire("k:rpm", 3, 3, 1))[0] for _ in range(5)]
    assert results == [True, True, True, False, False]


async def test_retry_after_is_time_until_enough_tokens(state):
    await state.acquire("k:rpm", 60, 60, 60)          # drain: refill is 1 token/sec
    ok, retry = await state.acquire("k:rpm", 60, 60, 5)
    assert not ok and 4.0 < retry <= 5.0


async def test_buckets_are_isolated(state):
    await state.acquire("a:rpm", 1, 1, 1)
    assert (await state.acquire("a:rpm", 1, 1, 1))[0] is False
    assert (await state.acquire("b:rpm", 1, 1, 1))[0] is True


async def test_cost_larger_than_remaining_rejected_without_consuming(state):
    await state.acquire("k:tpm", 100, 100, 70)
    assert (await state.acquire("k:tpm", 100, 100, 50))[0] is False
    assert (await state.acquire("k:tpm", 100, 100, 30))[0] is True  # the 30 left is still there


async def test_adjust_refunds_and_can_charge(state):
    await state.acquire("k:tpm", 100, 100, 100)
    await state.adjust("k:tpm", 100, 80)               # refund 80 of the estimate
    assert (await state.acquire("k:tpm", 100, 100, 70))[0] is True
    await state.adjust("k:tpm", 100, -500)             # actual usage far above estimate: debt
    assert (await state.acquire("k:tpm", 100, 100, 1))[0] is False


async def test_refill_over_time(state):
    await state.acquire("k:rpm", 600, 600, 600)        # 10 tokens/sec
    import asyncio
    await asyncio.sleep(0.25)
    assert (await state.acquire("k:rpm", 600, 600, 2))[0] is True


async def test_spend_and_paid_counter(state):
    assert await state.spend(1) == (0, 0)
    await state.add_spend(1, 2500)
    await state.add_spend(1, 500)
    assert await state.spend(1) == (3000, 3000) and await state.spend(2) == (0, 0)
    assert [await state.incr_paid_calls() for _ in range(3)] == [1, 2, 3]


async def test_window_count_and_marks(state):
    assert [await state.count("w", 1, 60) for _ in range(3)] == [1, 2, 3]
    assert await state.count("w", 5, 60) == 8 and await state.count("other", 1, 60) == 1
    assert await state.mark("m", 60) and not await state.mark("m", 60)
    assert await state.marked("m") and not await state.marked("nope")


async def test_windows_expire():
    now = [1000.0]
    s = MemoryState(clock=lambda: now[0])
    await s.count("w", 3, 60)
    await s.mark("m", 60)
    now[0] += 61
    assert await s.count("w", 1, 60) == 1 and not await s.marked("m") and await s.mark("m", 60)
