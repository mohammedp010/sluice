# Phase 4 notes

Run `bench/trace_demo.py` and read `bench/results/trace-failover.md` and ADR 0004 first. With
Docker running: `docker compose up -d jaeger`, then `python bench/trace_demo.py --otlp` and find
the trace at http://localhost:16686.

- In the span tree, what is the ~50ms gap between the two `chat big-model` attempts? Which
  setting controls it?
- Why are skipped providers events and not spans?
- Why does the request span of a stream end in `on_done` rather than when the handler returns?
  What would the latency histogram claim if it didn't?
- `sluice.team` is on the span but is forbidden on metrics. What is the difference in what
  each backend has to store per distinct value?
- The demo bills 6 input and 6 output tokens at $0.15 / $0.60 per million: that's 4.5 micro-USD,
  but the ledger says 4. Where did half a micro-dollar go, and does it matter at a billion
  requests?
- The GenAI conventions moved repo and renamed `gen_ai.system` while this was being built. What
  breaks downstream if you import attribute names from the SDK instead of pinning them?
- Redis spend and ledger spend are two records of the same money. Which one is "true"?
