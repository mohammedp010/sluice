# Phase 2 notes

Run `bench/chaos.py` and read `bench/results/chaos.md` before filling this in.

- Why does the breaker only count *consecutive* failures?
- Why full jitter on backoff instead of fixed delays?
- In the "flaky 30%" row, why is p95 so much worse than p50?
- What would go wrong if 400s counted against the breaker?
