# Sluice

An OpenAI-compatible LLM gateway built phase by phase as a learning project (see `PLAN.md`).
Free to run: mock + local Ollama by default; paid providers are blocked unless opted in.

**Done so far:** Phase 1 (gateway + streaming) · Phase 2 (fallback, retries, circuit breaker, chaos tests) ·
Phase 3 (virtual keys, RPM/TPM limits, budgets, kill switch).

## Quick start
```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m sluice.admin create-key --name demo --rpm 30 --daily-budget 0.01
.venv/bin/uvicorn sluice.api.app:app_factory --factory --port 8000
curl localhost:8000/v1/chat/completions -H "Authorization: Bearer <key>" -H 'content-type: application/json' \
  -d '{"model":"mock-model","messages":[{"role":"user","content":"hi"}]}'
```
Tests: `redis-server --port 6399 --save "" &` then `.venv/bin/pytest` (Redis tests skip if it's absent).
Benchmarks: `bench/chaos.py`, `bench/ratelimit.py` → `bench/results/`.
Design decisions: `docs/decisions/`. Per-phase notes: `docs/learning/`.
