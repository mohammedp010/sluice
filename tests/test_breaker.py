from sluice.router.breaker import CircuitBreaker, State


class Clock:
    t = 0.0

    def __call__(self):
        return self.t


def make(threshold=3, cooldown=10):
    clk = Clock()
    return CircuitBreaker(threshold, cooldown, clock=clk), clk


def test_closed_until_threshold():
    b, _ = make()
    b.record_failure(); b.record_failure()
    assert b.state is State.CLOSED and b.allow()
    b.record_failure()
    assert b.state is State.OPEN and not b.allow()


def test_success_resets_failure_count():
    b, _ = make()
    b.record_failure(); b.record_failure(); b.record_success()
    b.record_failure(); b.record_failure()
    assert b.state is State.CLOSED  # consecutive failures only


def test_open_to_half_open_after_cooldown_single_probe():
    b, clk = make()
    for _ in range(3):
        b.record_failure()
    clk.t = 9.9
    assert not b.allow()
    clk.t = 10
    assert b.state is State.HALF_OPEN
    assert b.allow()        # the probe
    assert not b.allow()    # everyone else still blocked while it's in flight


def test_probe_success_closes():
    b, clk = make()
    for _ in range(3):
        b.record_failure()
    clk.t = 10
    assert b.allow()
    b.record_success()
    assert b.state is State.CLOSED and b.allow()


def test_probe_failure_reopens_and_restarts_cooldown():
    b, clk = make()
    for _ in range(3):
        b.record_failure()
    clk.t = 10
    assert b.allow()
    b.record_failure()
    assert b.state is State.OPEN
    clk.t = 19.9
    assert b.state is State.OPEN
    clk.t = 20
    assert b.state is State.HALF_OPEN
