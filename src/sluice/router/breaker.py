"""Hand-written circuit breaker.

        success                      failures >= threshold
   +---------------+              +---------------------------+
   v               |              |                           v
 CLOSED --------------------------+                         OPEN
   ^                                                          |
   |  probe succeeds                          cooldown elapsed |
   |                                                          v
   +-------------------- HALF_OPEN <--------------------------+
                         (one probe at a time; probe fails -> OPEN again)
"""
from __future__ import annotations

import time
from collections.abc import Callable
from enum import Enum


class State(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    def __init__(
        self,
        failure_threshold: int = 3,
        cooldown_s: float = 10.0,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.failure_threshold = failure_threshold
        self.cooldown_s = cooldown_s
        self._clock = clock
        self._state = State.CLOSED
        self._failures = 0
        self._opened_at = 0.0
        self._probe_in_flight = False

    @property
    def state(self) -> State:
        # OPEN decays to HALF_OPEN lazily on read; no background timer needed.
        if self._state is State.OPEN and self._clock() - self._opened_at >= self.cooldown_s:
            self._state = State.HALF_OPEN
            self._probe_in_flight = False
        return self._state

    def allow(self) -> bool:
        """Ask permission to send a request. In HALF_OPEN only one probe is let through."""
        st = self.state
        if st is State.CLOSED:
            return True
        if st is State.HALF_OPEN and not self._probe_in_flight:
            self._probe_in_flight = True
            return True
        return False

    def record_success(self) -> None:
        self._state = State.CLOSED
        self._failures = 0
        self._probe_in_flight = False

    def record_failure(self) -> None:
        if self._state is State.HALF_OPEN:
            self._trip()
            return
        self._failures += 1
        if self._failures >= self.failure_threshold:
            self._trip()

    def _trip(self) -> None:
        self._state = State.OPEN
        self._opened_at = self._clock()
        self._probe_in_flight = False
