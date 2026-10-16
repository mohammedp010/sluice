# ADR 0007: What benchmarking against LiteLLM found, and the two fixes

**Status:** accepted (Phase 7). The evidence:
- `bench/results/compare.md`: Sluice vs LiteLLM.
- `bench/results/httpx-pool-leak.md`: the leak, isolated.
- `bench/results/profile-before*` and `profile-after*`: pyinstrument output.

## 1. A brief stall could kill a provider for good (correctness bug)

**Seen:** in the first full run, a laptop stall during the 250 rps step (the load generator fell
3 s behind) made a burst of Sluice's upstream calls hit their per-attempt timeout. After that,
**every** request returned 504, even at 25 rps, for the remaining 13 minutes. LiteLLM, in the
same run, recovered.

**Cause:**
- The executor enforced attempt timeouts by cancelling the task (`asyncio.timeout`).
- With httpx 0.28.1 / httpcore 1.0.9 (the latest release), a request cancelled while its
  connection is being set up leaves a dead `CONNECTING` entry in the pool, permanently.
- About 20 accumulate per burst of cancellations. Once all 100 slots are dead, every request
  waits for a slot and times out.
- The breaker couldn't help: each half-open probe hit the same dead pool and re-opened it.
- `bench/httpx_pool_leak.py` reproduces it without Sluice: 22 → 39 → … → 100 stuck entries,
  then `PoolTimeout`. httpx's own timeouts leave 0.

**Fix:**
- `Provider.enforces_timeout`. The OpenAI-compatible adapter turns each attempt's budget
  (`ChatParams.timeout_s`) into an `httpx.Timeout`.
- The executor's cancellation becomes a late backstop (budget + 1 s), for adapters that
  misbehave.
- Defence in depth: on `PoolTimeout` the adapter replaces its client, so any leak from other
  cancellation sources (for example client disconnects) heals instead of persisting.
- Regression test: `tests/test_pool_leak.py` (real sockets). It fails with 52 dead slots if
  the fix is switched off.
- Side effect: for streams, httpx's read timeout now also limits the silence between chunks,
  which closes ADR 0002's "no idle timeout between stream chunks" gap.

**Lesson:** the bug only showed up under an open-loop load test that *overloaded* the gateway
and then kept going. A closed-loop test, or one that stops at the first error, would have missed
it.

## 2. Top profiling hotspot: one disk sync per request, on the event loop

**Seen:** at 100 rps, the flat profile put SQLite's commit (`Connection.__exit__`, 1.02 s) and
insert (0.55 s) at the top. That's ~19% of 8.4 s CPU, spent blocking the event loop, so every
in-flight request waited on each commit.

**Fix:**
- The usage ledger buffers rows in memory.
- A background task (started in the app's lifespan) writes them once a second, in one
  transaction, on a worker thread.
- Rows keep the time they were recorded, not the flush time.
- Readers flush first, so in-process reads see their own writes.
- A failed flush keeps the rows for the next try. Shutdown flushes what's left.

**Cost:** a crash loses up to ~1 s of ledger rows. Budget enforcement is unaffected (it uses the
limit store, Redis), so this is reporting data only.

**Not fixed:** the httpx client stack is the largest *total* cost, but it's spread over dozens of
library frames (connection-pool bookkeeping, `has_expired` checks per pooled connection per
request). Replacing the HTTP client (LiteLLM uses aiohttp for this reason) is a bigger change
than "fix the top hotspot".
