# Sluice

An OpenAI-compatible LLM gateway built phase by phase as a learning project (see `PLAN.md`).
Free to run: mock + local Ollama by default; paid providers are blocked unless opted in.

**Done so far:** Phase 1 (gateway + streaming) · Phase 2 (fallback, retries, circuit breaker, chaos tests) ·
Phase 3 (virtual keys, RPM/TPM limits, budgets, kill switch) · Phase 4 (OpenTelemetry traces + metrics,
per-team cost ledger) · Phase 5 (semantic cache with a lexical guard; threshold from a labelled eval).

## Quick start
```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m sluice.admin create-key --name demo --rpm 30 --daily-budget 0.01
.venv/bin/uvicorn sluice.api.app:app_factory --factory --port 8000
curl localhost:8000/v1/chat/completions -H "Authorization: Bearer <key>" -H 'content-type: application/json' \
  -d '{"model":"mock-model","messages":[{"role":"user","content":"hi"}]}'
```
Observability: every response has `x-sluice-trace-id`; `GET /metrics` (Prometheus);
`python -m sluice.report --by team|app|key|model|provider|day` for cost; traces in Jaeger with
`docker compose up -d jaeger` and `telemetry.traces: otlp` in the policy (ADR 0004).

Semantic cache: `ollama pull nomic-embed-text`, then opt a route in with `cache: true`. Responses
carry `x-sluice-cache: hit|miss|skip`; `Cache-Control: no-cache` / `no-store` are honoured.
Threshold evidence: `bench/results/cache-eval.md` (ADR 0005).

Tests: `redis-server --port 6399 --save "" &` then `.venv/bin/pytest` (Redis tests skip if it's absent).
Benchmarks: `bench/chaos.py`, `bench/ratelimit.py`, `bench/trace_demo.py`, `bench/cache_eval.py` → `bench/results/`.
Design decisions: `docs/decisions/`. Per-phase notes: `docs/learning/`.
