# Phase 7 notes

Read `bench/results/compare.md`, `httpx-pool-leak.md` and ADR 0007 first.

Re-run, in order:
1. `python bench/compare.py --tag mine` (~17 min; close other apps)
2. `python bench/compare_report.py mine`
3. `python bench/profile_sluice.py --tag mine`

- `bench/loadgen.py` measures latency from each request's *scheduled* time. Change it to measure
  from the actual send time and re-run at a rate that overloads Sluice. How much better does p99
  look? That gap is coordinated omission.
- Change it again to a closed loop (N workers, each waits for its reply). Why does it never
  overload anything, and why is that the problem?
- The load generator reports its own lag. Why is a benchmark that can't tell "the server was
  slow" from "the client was late" worse than no benchmark?
- The pool-leak bug only appeared after an overload followed by normal load. Which of your own
  tests would have caught it? What test would you add to a CI pipeline for this class of bug?
- The profiler shows wall time on the event-loop thread. Why does a 1 ms synchronous disk
  commit hurt *other* requests' latency more than its own?
- Batching the ledger trades durability (up to ~1 s of rows on a crash) for loop time. Where
  else in Sluice is that trade made, or should be?
- Read the httpx part of the flat profile. What is `has_expired` doing on every request, and why
  does its cost grow with the pool size?
- Every number here comes from one laptop. Which conclusions would survive moving to a 32-core
  server, and which wouldn't?
