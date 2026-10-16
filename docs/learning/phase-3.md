# Phase 3 notes

Run `bench/ratelimit.py` (needs redis on SLUICE_TEST_REDIS) and read ADR 0003 first.

- Why is the Lua script needed? What race exists with GET -> compute -> SET from Python?
- Why use Redis TIME instead of the gateway's clock?
- The first burst test crashed with "Too many connections". What does that tell you about the
  gap between a passing unit test and real concurrency?
- Why is a token bucket able to refund, but a fixed-window counter awkward at it?
- Fail open vs fail closed on Redis loss: when would you choose the other one?
