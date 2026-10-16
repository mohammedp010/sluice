# Sluice: a learn-by-building LLM gateway

> A sluice is a gate that controls how much flows through, where it goes, and when to shut it. That is what this gateway does for LLM traffic.

Derived from `p2-README.md`. That document is the "what and why". This one is the "how, in what order, and at what cost (zero)".

---

## 1. Analysis of the original plan

### What is strong
- Clear gap: infrastructure layer (routing, resilience, cost, tracing) rather than another agent/RAG demo.
- Honest evaluation mindset: every claim needs a test, no borrowed benchmark numbers.
- Sensible non-goals (no 100+ providers, no UI, no A2A).
- Phase order is mostly right: you can't rate-limit or trace what doesn't exist yet.

### What needs changing for this project's goals
| Issue in original | Why it matters here | Change |
|---|---|---|
| Azure OpenAI + Bedrock as primary providers | Paid, need cloud accounts, easy to leak spend | Put them behind the same adapter interface, but develop and test against **Ollama + a Mock provider + free-tier APIs**. Azure/Bedrock adapters are written last, tested only with recorded responses / a tiny optional smoke test. |
| "Test against real providers" for failover/load | Real providers can't be killed on demand, and load tests burn tokens | **Mock provider** with injectable latency, errors, 429s, mid-stream cuts. Deterministic and free. This is also *better* for learning: you can reproduce failures exactly. |
| LLM-as-judge for quality-aware routing | Every request costs 2 calls | Start with free signals (heuristics, logprob/length/refusal checks); use a local small model as judge; judge on a sampled subset only. |
| Semantic cache via embeddings API | Paid per call | Local embeddings (`sentence-transformers` MiniLM, or Ollama `nomic-embed-text`). |
| LiteLLM benchmark on real providers | Noisy and costly | Benchmark both gateways against the **same mock upstream**. Measures gateway overhead in isolation, which is the fair thing to measure anyway. |
| Langfuse, Terraform, pgvector all at once | Lots of ops before any value | Introduce each only in the phase that needs it. Start with SQLite/in-memory; move to Postgres+pgvector in the cache phase. |
| 3-4 weekends for phases 1-7 | Optimistic when the goal is learning | Plan ~6-8 weekends, each phase ends with a written "what I learned" note. |

### Real gaps in the original
- No spec for **retry vs fallback semantics** (what is idempotent? what counts as retryable?). Defined in Phase 2.
- **Token counting** is hand-waved. Needs a decision (tiktoken approximate vs provider-reported usage). Phase 3.
- No **config format / policy schema** decided. Phase 1 picks YAML + pydantic validation.
- **Security basics** missing: key hashing, secrets handling, request size limits, log redaction. Added to Phase 3.
- Streaming failover is the "hardest decision". Decision proposed below so it isn't left open.

### Proposed decision: streaming failover
- **Before the first byte** is sent to the client: fail over freely (client sees nothing).
- **After the first byte**: do not silently restart (client would see duplicated/inconsistent text). Emit a terminal SSE error event with a machine-readable reason, and record it in the trace.
- Optional later experiment: opt-in "restart with prefix" mode, behind a flag, measured for quality.
This is simple, honest, and matches what most production gateways do.

---

## 2. Zero-cost rules (the budget policy)

1. **Default provider is Mock, then Ollama.** Nothing in CI or unit tests touches a network provider.
2. **Real hosted APIs are opt-in** via an env var (`SLUICE_ALLOW_PAID=1`), off by default.
3. **Free tiers only** for real-API checks: e.g. Groq, Gemini API free tier, OpenRouter `:free` models. Check current limits yourself; they change.
4. **Hard spend guard in code**: a global kill switch that refuses any non-local provider call after N calls per day (Phase 3 budget logic reuses this, so it's not wasted work).
5. **Record & replay**: capture a handful of real responses once (VCR-style fixtures), then replay forever.
6. **Small prompts, small models, `max_tokens` capped** everywhere by default.
7. A real-API run is a deliberate, logged act (< 50 calls), never part of a loop.

### Provider tiers
| Tier | Provider | Use |
|---|---|---|
| 0 | Mock (built in) | all dev, tests, chaos, load tests, LiteLLM comparison |
| 1 | Ollama (local, e.g. a 1-3B model) | real token streaming, embeddings, local judge, last-resort fallback |
| 2 | Free-tier hosted API (one) | proves multi-provider routing against a genuinely remote provider |
| 3 | Azure OpenAI / Bedrock | adapters implemented and unit-tested with recorded fixtures; live smoke test optional |

---

## 3. Stack (kept minimal, added when needed)

| Concern | Choice | Introduced in |
|---|---|---|
| API | FastAPI + uvicorn, `httpx` async client | Phase 1 |
| Config | YAML + pydantic v2 | Phase 1 |
| Tests | pytest, pytest-asyncio, respx/httpx mock | Phase 1 |
| State (keys, usage) | SQLite via SQLAlchemy | Phase 3 |
| Rate-limit state | Redis (Docker), Lua scripts | Phase 3 |
| Telemetry | OpenTelemetry SDK, console/OTLP -> Jaeger or Langfuse self-hosted | Phase 4 |
| Cache | Postgres + pgvector (Docker), local embeddings | Phase 5 |
| Load testing | `locust` or `k6` | Phase 7 |
| Packaging | Docker Compose | Phase 3 onward |

Skip Terraform unless there's a real cloud deploy you want to learn from; it is not on the critical path.

---

## 4. Repository layout (target)

```
sluice/
  src/sluice/
    api/            # FastAPI routes, OpenAI-compatible schemas, SSE
    providers/      # base.py, mock.py, ollama.py, openai_compat.py, azure.py, bedrock.py
    router/         # policy loading, chain execution, circuit breaker
    limits/         # rate limiter, budgets
    auth/           # virtual keys
    cache/          # semantic cache
    quality/        # escalation signals
    telemetry/      # otel setup, cost table
    config.py
  policies/         # routes.yaml
  tests/
  bench/            # load tests, chaos scripts, results/
  docs/
    decisions/      # ADRs, one per big choice
    learning/       # one note per phase
  docker-compose.yml
  PLAN.md
```

---

## 5. Phases

Each phase has: **Goal**, **Learn**, **Build**, **Cost**, **Done when**. Every phase ends with a short note in `docs/learning/phase-N.md` (what surprised me, what I'd do differently) and any big choice recorded as an ADR.

### Phase 0: Foundations (half a weekend)
- **Goal:** repo, tooling, a fixed vocabulary.
- **Learn:** the OpenAI chat-completions wire format, including the SSE chunk format (`data: {...}\n\n`, `[DONE]`).
- **Build:**
  - Repo scaffold, `pyproject`, linter, pytest, pre-commit.
  - Install Ollama, pull one small model, call it with plain `curl` and with the OpenAI Python SDK pointed at `http://localhost:11434/v1`.
  - Write `docs/openai-format.md` by hand from observing real responses.
- **Cost:** $0.
- **Done when:** you can explain every field of a streaming chunk from memory.

### Phase 1: Minimal gateway (1 weekend)
- **Goal:** `POST /v1/chat/completions` works, streaming and non-streaming, against Mock and Ollama.
- **Learn:** ASGI, async generators, SSE, backpressure, client disconnects, timeouts in `httpx`.
- **Build:**
  - Provider interface: `complete()` and `stream()` returning a normalized internal type.
  - `MockProvider` with knobs: latency, tokens/sec, error rate, fixed text, cut stream after N tokens.
  - `OllamaProvider` (via its OpenAI-compatible endpoint).
  - Request/response pydantic models, `/v1/models`, `/healthz`.
  - Routing by `model` name from `routes.yaml`.
- **Cost:** $0.
- **Done when:** the official `openai` SDK works unchanged against Sluice for both providers, with streaming; tests cover the happy path and client disconnect.

### Phase 2: Resilience (1 weekend)
- **Goal:** ordered fallback chain, retries, circuit breaker, proven under injected faults.
- **Learn:** timeout budgets, retry-with-jitter, idempotency, circuit breaker state machine (closed/open/half-open), tail latency.
- **Build:**
  - Error taxonomy: retryable (timeout, 5xx, 429) vs not (400, 401, content policy).
  - Per-route chain, per-provider timeout, total deadline.
  - Circuit breaker implemented by hand (do not import a library; the point is learning).
  - Streaming failover rule from section 1 (pre-first-byte only), ADR written.
  - Chaos script driving MockProvider: kill primary, add 3s latency, return 429s, cut stream.
- **Cost:** $0.
- **Done when:** under a chaos run, success rate and recovery time are measured and written to `bench/results/`; unit tests cover each breaker transition.

### Phase 3: Keys, limits, budgets (1-1.5 weekends)
- **Goal:** the gateway knows *who* is calling and how much they may use.
- **Learn:** token bucket vs sliding window, atomic ops in Redis (Lua), estimate-then-reconcile accounting, secure key storage.
- **Build:**
  - Virtual keys: generated, stored **hashed**, tied to team/app, allowed models, limits.
  - RPM + TPM limiter in Redis (estimate prompt tokens up front with `tiktoken`/approx, reconcile with reported usage).
  - Daily/monthly budget per key from a static price table (fake prices are fine for local models: treat "cost" as a configured number so the logic is exercised).
  - Global paid-provider kill switch (section 2, rule 4).
  - Basics: request size cap, log redaction of auth headers.
  - Docker Compose with gateway + Redis.
- **Cost:** $0.
- **Done when:** burst test from several keys shows correct 429s with `Retry-After`, zero cross-key leakage, and limits hold across two gateway replicas.

### Phase 4: Observability and cost attribution (1 weekend)
- **Goal:** any request, including a failed-over one, can be explained from its trace.
- **Learn:** OpenTelemetry spans/metrics, context propagation, GenAI semantic conventions and their (unstable) status, cardinality pitfalls.
- **Build:**
  - Spans: request, auth/limits, cache (stub), each provider attempt, fallback reason.
  - Attributes per GenAI conventions, **version pinned** in the repo with a note on its status.
  - Metrics: request count, latency histograms, tokens, cost, fallback count, breaker state.
  - Cost aggregation by team/app via SQLite + a tiny report script (no UI).
  - Jaeger (simplest) first; Langfuse self-hosted only if you want to learn it.
- **Cost:** $0.
- **Done when:** one screenshot/export shows a request that failed over and its full path with cost attributed.

### Phase 5: Semantic cache (1-1.5 weekends)
- **Goal:** reduce repeated work *without* returning wrong answers.
- **Learn:** embeddings, cosine similarity, ANN indexes, threshold tuning, precision vs recall, cache-key scoping.
- **Build:**
  - Local embedding model; Postgres + pgvector (or start with in-memory numpy to learn the maths, then migrate).
  - Cache scoped per tenant + model + relevant params (temperature, system prompt hash).
  - Skip rules: streaming policy, user-specific data, `temperature > 0` option, TTL.
  - Labelled eval set (~100-200 prompt pairs, hand-labelled "same intent / different intent") so you can plot precision/recall vs threshold.
  - Report hit rate **and** false-hit rate.
- **Cost:** $0 (labelling is manual time, not money).
- **Done when:** a threshold is chosen from a plotted curve and the false-hit rate is reported on a held-out set.

### Phase 6: Quality-aware routing (1-1.5 weekends)
- **Goal:** cheap model first, escalate only when needed, measure what it saved and what quality it lost.
- **Learn:** evaluation design, judge bias, cost/quality trade-off curves, why "cheap check" is the hard part.
- **Build:** try signals in increasing order of cost, keep a table of which won:
  1. Heuristics: empty/too short, refusal phrases, malformed JSON when JSON was requested, repetition loops.
  2. Self-consistency / confidence proxy (logprobs if the provider exposes them).
  3. Local small-model judge on a sampled fraction.
  - "Cheap" and "strong" can be two Ollama models of different sizes, so there is a real quality gap at no cost.
  - Offline eval on a fixed mixed easy/hard prompt set versus an always-strong baseline.
- **Cost:** $0 (local compute only).
- **Done when:** you have a cost-vs-quality plot with the always-cheap, always-strong and routed points, and a written conclusion even if the answer is "it didn't help".

### Phase 7: Benchmark and write-up (1 weekend)
- **Goal:** honest overhead and feature comparison, publishable repo.
- **Learn:** load-test methodology (coordinated omission, warm-up, percentiles), reading flamegraphs.
- **Build:**
  - Same mock upstream with fixed latency; run Sluice and LiteLLM (self-hosted) on the same machine at stated RPS.
  - Report p50/p95/p99 added latency, error behaviour under injected failures, and a feature-gap table that includes where Sluice loses.
  - Profile once (`py-spy`) and fix the top hotspot.
  - README, architecture diagram, design-decisions write-up.
- **Cost:** $0.
- **Done when:** the definition-of-done list in section 7 is satisfied.

### Phase 8 (stretch, only if 1-7 are done)
- Azure OpenAI / Bedrock adapters hardened with recorded fixtures and one tiny live smoke test.
- MCP proxy with per-key tool permissions and audit log.
- Opt-in streaming "restart with prefix" mode, measured.

---

## 6. Timeline (realistic, learning-first)

| Phase | Weekends | Cumulative |
|---|---|---|
| 0 Foundations | 0.5 | 0.5 |
| 1 Minimal gateway | 1 | 1.5 |
| 2 Resilience | 1 | 2.5 |
| 3 Keys/limits/budgets | 1.5 | 4 |
| 4 Observability | 1 | 5 |
| 5 Semantic cache | 1.5 | 6.5 |
| 6 Quality routing | 1.5 | 8 |
| 7 Benchmark/write-up | 1 | 9 |

Phases are ordered by dependency, but 4 (tracing) can move earlier if you want to see what you're building from Phase 2 onward. It's a good idea; the cost is small.

---

## 7. Definition of done

- Failover demonstrated under injected failure, with numbers in `bench/results/`.
- Streaming failure behaviour documented and tested (ADR).
- Limits verified across multiple instances.
- Cache false-hit rate reported alongside hit rate.
- Routing result reported honestly, including negative results.
- Comparison with LiteLLM on the same mock upstream, including weaknesses.
- Every number in the README links to a script and a result file in this repo.
- Total money spent on LLM APIs: **$0** (or a number you can state exactly).

---

## 8. Risks specific to the free/learning approach

| Risk | Mitigation |
|---|---|
| Mock behaviour diverges from real providers | Record real responses once (Tier 1/2) and make Mock replay their shapes, including error bodies and SSE quirks. |
| Small local models give noisy quality signals | Use many prompts and report variance; treat Phase 6 as an experiment, not a promise. |
| Laptop load tests are noisy | Pin CPU-heavy things, run multiple times, report ranges, state the hardware. |
| Free-tier limits change or vanish | Nothing in the core path depends on them. |
| Over-building (MCP, extra providers, UI) | Phase 8 is gated on 1-7 being done. |
| Copying patterns without understanding | Hand-write breaker, limiter and cache before reading how LiteLLM does it; compare afterwards. |

---

## 9. Learning method (apply every phase)

1. Read the concept first (a short doc or paper, not a tutorial video).
2. Write a failing test that states the behaviour.
3. Implement by hand, no library that hides the idea.
4. Break it on purpose (chaos, bad input).
5. Write the note: what I believed, what happened, what I now believe.

## 10. Immediate next steps

1. Create the repo skeleton from section 4.
2. Install Ollama and pull one small model.
3. Start Phase 0, then Phase 1.
