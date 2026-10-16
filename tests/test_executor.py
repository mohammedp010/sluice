import pytest

from sluice.config import Route
from sluice.providers.base import ChatParams, Message
from sluice.providers.mock import MockProvider
from sluice.router.breaker import CircuitBreaker, State
from sluice.router.executor import AllProvidersFailed, Executor, NonRetryableError

PARAMS = ChatParams(model="", messages=[Message("user", "hi")])


async def no_sleep(_):
    return None


def setup(primary=None, secondary=None, retries=1, timeout_s=1.0, deadline_s=10, threshold=3):
    provs = {"a": primary or MockProvider("a", text="from a"),
             "b": secondary or MockProvider("b", text="from b")}
    breakers = {n: CircuitBreaker(threshold, 60) for n in provs}
    ex = Executor(provs, breakers, sleep=no_sleep)
    route = Route(deadline_s=deadline_s, chain=[
        {"provider": "a", "upstream_model": "m", "retries": retries, "timeout_s": timeout_s},
        {"provider": "b", "upstream_model": "m", "retries": 0, "timeout_s": timeout_s},
    ])
    return ex, route


async def test_primary_ok_no_failover():
    ex, route = setup()
    c, trail = await ex.complete(route, PARAMS)
    assert c.content == "from a" and trail.provider == "a" and len(trail.attempts) == 1


async def test_retry_same_target_before_failover():
    a = MockProvider("a", text="from a", error_rate=1.0)
    ex, route = setup(primary=a, retries=2)
    c, trail = await ex.complete(route, PARAMS)
    assert c.content == "from b"
    assert [x.provider for x in trail.attempts] == ["a", "a", "a", "b"]  # 1 + 2 retries, then b


async def test_transient_failure_recovers_via_retry():
    a = MockProvider("a", text="from a", error_rate=1.0)
    ex, route = setup(primary=a, retries=1)

    orig = a._maybe_fail
    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            await orig()
    a._maybe_fail = flaky
    c, trail = await ex.complete(route, PARAMS)
    assert c.content == "from a" and len(trail.attempts) == 2


async def test_non_retryable_does_not_retry_or_failover_or_trip_breaker():
    a = MockProvider("a", error_rate=1.0, error_status=400)
    ex, route = setup(primary=a, retries=3, threshold=1)
    with pytest.raises(NonRetryableError) as e:
        await ex.complete(route, PARAMS)
    assert e.value.error.status == 400 and len(e.value.trail.attempts) == 1
    assert ex.breakers["a"].state is State.CLOSED


async def test_429_is_retryable():
    a = MockProvider("a", error_rate=1.0, error_status=429)
    ex, route = setup(primary=a, retries=0)
    c, _ = await ex.complete(route, PARAMS)
    assert c.content == "from b"


async def test_all_fail():
    ex, route = setup(MockProvider("a", error_rate=1.0), MockProvider("b", error_rate=1.0))
    with pytest.raises(AllProvidersFailed) as e:
        await ex.complete(route, PARAMS)
    assert len(e.value.trail.attempts) == 3


async def test_timeout_triggers_failover():
    ex, route = setup(primary=MockProvider("a", latency_ms=300), retries=0, timeout_s=0.05)
    c, trail = await ex.complete(route, PARAMS)
    assert c.content == "from b" and trail.attempts[0].outcome == "timeout"


async def test_open_breaker_skips_provider_without_calling_it():
    a = MockProvider("a", error_rate=1.0)
    ex, route = setup(primary=a, retries=0, threshold=2)
    for _ in range(2):
        await ex.complete(route, PARAMS)
    assert ex.breakers["a"].state is State.OPEN
    a.error_rate = 0.0  # even healthy, it must be skipped while open
    _, trail = await ex.complete(route, PARAMS)
    assert [(x.provider, x.outcome) for x in trail.attempts] == [("a", "skipped_open"), ("b", "ok")]


async def test_deadline_bounds_total_time():
    ex, route = setup(MockProvider("a", latency_ms=500), MockProvider("b", latency_ms=500),
                      retries=0, timeout_s=5, deadline_s=0.1)
    with pytest.raises(AllProvidersFailed) as e:
        await ex.complete(route, PARAMS)
    assert e.value.trail.attempts[0].outcome == "timeout"


async def test_stream_fails_over_before_first_chunk():
    ex, route = setup(primary=MockProvider("a", error_rate=1.0), retries=0)
    (first, rest), trail = await ex.stream(route, PARAMS)
    text = first.content + "".join([c.content async for c in rest])
    assert text == "from b" and trail.provider == "b"


async def test_stream_does_not_fail_over_after_first_chunk():
    from sluice.providers.base import ProviderError
    ex, route = setup(primary=MockProvider("a", text="x y z", cut_stream_after=1), retries=0)
    (_, rest), trail = await ex.stream(route, PARAMS)
    assert trail.provider == "a"
    with pytest.raises(ProviderError):
        [c async for c in rest]


async def test_half_open_probe_freed_after_non_retryable_error():
    # A 400 during the half-open probe says nothing about health; the probe slot must be freed
    # or the breaker rejects every request forever.
    a = MockProvider("a", error_rate=1.0)
    ex, route = setup(primary=a, retries=0, threshold=1)
    await ex.complete(route, PARAMS)
    br = ex.breakers["a"]
    br._opened_at -= 120  # cooldown elapsed -> half-open
    a.error_status = 400
    with pytest.raises(NonRetryableError):
        await ex.complete(route, PARAMS)
    a.error_rate = 0.0
    _, trail = await ex.complete(route, PARAMS)
    assert trail.provider == "a" and br.state is State.CLOSED


async def test_cancelled_call_frees_half_open_probe():
    import asyncio
    a = MockProvider("a", error_rate=1.0)
    ex, route = setup(primary=a, retries=0, threshold=1)
    await ex.complete(route, PARAMS)
    ex.breakers["a"]._opened_at -= 120
    a.error_rate, a.latency_ms = 0.0, 1000
    task = asyncio.create_task(ex.complete(route, PARAMS))
    await asyncio.sleep(0.01)
    task.cancel()  # the client went away mid-call
    with pytest.raises(asyncio.CancelledError):
        await task
    assert ex.breakers["a"].allow()


async def test_upstream_auth_error_fails_over_instead_of_reaching_client():
    # a 401 from the provider means OUR provider key is bad: try the next target, count it
    a = MockProvider("a", error_rate=1.0, error_status=401)
    ex, route = setup(primary=a, retries=3, threshold=1)
    c, trail = await ex.complete(route, PARAMS)
    assert c.content == "from b" and [x.provider for x in trail.attempts] == ["a", "b"]
    assert ex.breakers["a"].state is State.OPEN
