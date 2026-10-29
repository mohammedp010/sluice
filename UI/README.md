# Sluice Workbench

A devtools-style UI for seeing what Sluice does. You send a real request through the running
gateway, then inspect its path, spans, headers and cost. From there you can follow any stage to
the measured evidence and the decision record behind it.

## Run it

```bash
# 1. the gateway, with the lab policy (turns on the read-only admin API + fault-injecting routes)
cd ..   # repo root
.venv/bin/python -m sluice.admin create-key --name lab --team search --rpm 120   # copy the key
SLUICE_POLICY=policies/lab.yaml .venv/bin/uvicorn sluice.api.app:app_factory --factory --port 8000

# 2. the UI
cd UI && npm install && npm run dev      # http://localhost:5173
```

Paste the key into the Lab's key field. It's kept in this browser's localStorage only. The dev
server proxies `/v1`, `/mcp`, `/metrics` and `/healthz` to `localhost:8000`, or to `SLUICE_URL`
if you set it. The admin API answers loopback callers only, which the dev server is. If the
gateway runs with `SLUICE_ADMIN_TOKEN`, start the dev server with the same variable and it adds
the token to admin requests itself; the browser never sees it (ADR 0010). Evidence, Decisions and Build log read repo files at build time, so they work
with the gateway off.

## Panels

| Panel | Shows | Source |
|---|---|---|
| Lab | Send a request; the pipeline map replays its path step by step; span waterfall at real timings; inspector with trail, response, headers, ledger row, raw span | `/v1/chat/completions`, `/v1/sluice/admin/traces/{id}` |
| Topology | Routes and their fallback chains, providers with live breakers, every gateway setting | `/v1/sluice/admin/overview` |
| Ledger | Spend by team / app / key / model / provider / day; recent rows open in the Lab | `/v1/sluice/admin/ledger*` |
| Evidence | Every README claim, charted from its `bench/results/` file, including negative results and losses | `bench/results/*` |
| Decisions | ADRs 0001–0009 | `docs/decisions/` |
| Build log | Commits as revisions, with each phase's ADRs and learning-note questions | `git log`, `docs/learning/` |
| MCP | Tool grants per key (glob matrix) and the audit log | `/v1/sluice/admin/mcp`, `/keys` |
| Keys | Limits, budgets with live spend, model and tool grants (never the key) | `/v1/sluice/admin/keys` |

## Lab routes (`policies/lab.yaml`)

The `lab-*` routes are all mocks, so they cost $0:

- `lab-failover`: the primary always answers 503. It is retried once, then the backup serves. Send it 5× and the primary's breaker opens.
- `lab-flaky`: the primary fails 40% of the time.
- `lab-stream-cut`: streams die after 12 tokens and resume on the backup (`prefill`, ADR 0008).
- `lab-stream-cut-off`: the same cut with resume off: a terminal error event and no `[DONE]` (ADR 0001).
- `lab-escalate`: the cheap model answers empty, fails the `empty` check, and the request is re-asked on the strong route (ADR 0006).
- `mock-model` (with the semantic cache on), `local-small` and `auto` run on local Ollama, as in `routes.yaml`.
