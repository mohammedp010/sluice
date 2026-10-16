# ADR 0004: Traces, metrics and cost attribution

**Status:** accepted (Phase 4).

- **Three outputs, three jobs.**
  - *Traces:* explain one request, including its failovers.
  - *Metrics:* show how the fleet is doing in aggregate.
  - *SQLite usage ledger:* say who spent what.

  They are written together, exactly once per request, by `finish()` in `api/app.py`. A
  `trace_id` column joins a cost row to its trace (`python -m sluice.report --trace <id>`), and
  every chat response carries `x-sluice-trace-id`.
- **Span model:**
  - The request span (SERVER, named `chat <public model>`) has children `sluice.auth`,
    `sluice.limits` and `sluice.cache` (a stub until Phase 5).
  - Each upstream attempt gets its own CLIENT span, `chat <upstream model>`, with an
    `error.type` set when it fails.
  - Skips (open breaker, paid guard) and target changes are `sluice.fallback` *events* on the
    request span, not spans, because nothing was called.
  - The backoff sleep is the visible gap between two attempt spans.
- **Streams:** an attempt span covers connect plus time to first chunk, which is where failover
  can still happen (ADR 0001). The request span stays open until the last byte, a mid-stream
  error or a client disconnect. Its `sluice.outcome` is `ok`, `stream_interrupted` (span
  status ERROR) or `client_disconnected`.
- **Conventions:** GenAI attribute names are spelled out in `telemetry/semconv.py`, pinned to
  schema 1.44.0.
  - These names are *Development* status. In that release they are marked as moved to the
    separate `semantic-conventions-genai` repo, and they were renamed once already
    (`gen_ai.system` became `gen_ai.provider.name`).
  - Pinning means an SDK upgrade can't silently rename what dashboards query.
  - `gen_ai.provider.name` holds our provider *config* name (`ollama`, `mock`), not one of the
    well-known values. It's the useful label for a gateway, and a deliberate deviation.
- **Propagation:** a W3C `traceparent` header from a caller is honoured, so Sluice joins the
  caller's trace. Upstream calls get a `traceparent` whose parent is the attempt span.
- **Cardinality:** metric attributes are bounded sets only: public model, provider, outcome,
  status and error type. Key, team and app are *not* metric labels, because one series per key
  grows without bound. Per-key and per-team cost comes from the ledger instead. A test enforces
  this.
- **Histogram buckets:** these use the GenAI-recommended boundaries (seconds: 0.01 to 82,
  tokens: 1 to 4M). The SDK's default boundaries are tuned for milliseconds and would put every
  LLM call in the overflow bucket.
- **Export:**
  - Traces go via OTLP/HTTP to Jaeger v2 (`docker compose up jaeger`, UI on :16686), through a
    batch processor so export never blocks a request.
  - Metrics are pulled: Prometheus format on `GET /metrics`.
  - Defaults are `traces: none` locally and `otlp` in compose.
- **Ledger:** one row per request that reached the executor. Rejected requests (401/403/404,
  rate and budget limits) are traced and counted but cost nothing, so they get no row.
  - Team and app are copied onto the row, so spend stays attributed to the owner *at the time*.
  - The write is synchronous SQLite (sub-millisecond at this scale). Rollback journal plus
    busy timeout, not WAL, because WAL is unreliable on Docker Desktop bind mounts shared by
    two replicas.
  - A failed ledger write is recorded on the span but never fails a served request.
- **Known gaps:**
  - No sampling: every request is traced. Fine for a laptop; Phase 7 will measure the cost.
  - `/metrics` is unauthenticated.
  - Spend in Redis (enforcement) and the ledger (reporting) are two separate records of the same
    money. They agree when the price table hasn't changed, but nothing reconciles them.
  - Ledger cost is rounded to whole micro-USD per request.
