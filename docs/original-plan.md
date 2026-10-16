# LLM Gateway

An OpenAI-compatible AI gateway that sits between applications and LLM providers. It handles multi-provider routing, failover, token-aware rate limiting, budget control, semantic caching, and OpenTelemetry-based observability.

**Status:** Planning. This document covers the idea, scope, architecture and build plan. No implementation yet.

---

## 1. Overview

Production LLM systems depend on a layer that most demos skip: something that decides which provider handles a request, what happens when a provider fails, who is allowed to spend how much, and how every call is traced and costed.

This project builds that layer. Applications point at one endpoint and get:

- Routing across Azure OpenAI, AWS Bedrock and a local Ollama model
- Automatic fallback when a provider is slow, rate-limited or down
- Per-key limits on both requests and tokens, plus spend caps
- Semantic caching to avoid paying for repeated questions
- Full tracing with cost, latency and provider attribution on every request
- Streaming (SSE) that keeps working during failover

## 2. Why this project

### 2.1 Gap it fills
Existing portfolio work already covers agent orchestration, RAG tuning, guardrails and evaluation. It does not yet show the infrastructure layer underneath production LLM services: routing, resilience, cost control and tracing. This project covers that, using the same stack as day-to-day platform work (FastAPI, Azure, Bedrock, SSE, Docker, Terraform).

### 2.2 Industry context (research findings, October 2026)

- **The category has moved from "LLM proxy" to "AI gateway."** A 2024 gateway proxied provider calls and added retries. Current comparisons treat multi-provider routing alone as no longer differentiating. What matters now is agent protocol coverage (MCP and A2A), tool-permission enforcement, OpenTelemetry output and cost governance.
- **Agent traffic is the driver.** A single agent run can touch several LLM providers, many MCP tool servers and internal APIs in one user turn. Gateways are expanding to govern all of it. Kong's Agent Gateway (April 2026) added agent-to-agent traffic governance alongside LLM and MCP traffic.
- **Consolidation and maturity.** Palo Alto Networks completed its Portkey acquisition on 29 May 2026. Portkey open-sourced most of its gateway in March 2026. Envoy AI Gateway reached v1.0 in June 2026. LiteLLM remains the most widely used open-source option.
- **Observability is standardising, but not finished.** OpenTelemetry's GenAI and MCP semantic conventions moved to a dedicated repository in June 2026 and are still marked Development. Some sources describe them as stabilised, but the more recent and more detailed ones say they are not. This project pins a version and states the status openly.
- **Cost control is a gateway job.** The usual levers are semantic caching, cost-aware model routing, token and rate limits, and per-team cost attribution. Reported savings vary widely by workload. Figures seen range from 20 to 73 percent on token cost, with some sources reporting 30 to 50 percent cache hit rates for high-volume applications.
- **Tool schemas are a growing token cost.** As agents connect to more tools, loading every tool definition into context becomes expensive. This is a possible stretch area, not a core goal.

### 2.3 A note on evidence quality
Most of the numbers above come from gateway vendors writing about their own products. They show direction, not guarantees. **No external benchmark figure should be reused as a claim about this project.** Every number in the final write-up must come from this project's own measurements.

## 3. Goals and non-goals

### Goals
1. A working gateway with an OpenAI-compatible API and streaming support
2. Resilient failover that is demonstrated, not just configured
3. Token-aware rate limiting and budget enforcement per virtual key
4. Semantic caching with a measured false-hit rate
5. Tracing and cost attribution using OpenTelemetry GenAI conventions
6. An honest benchmark against an existing gateway (LiteLLM)
7. One differentiating feature: quality-aware routing

### Non-goals
- Supporting 100+ providers (existing gateways already do this)
- A managed SaaS offering or a polished admin UI
- Building a new evaluation framework
- Full agent-to-agent (A2A) governance

## 4. Architecture overview

```
Clients and agents
        |
[ Gateway API: OpenAI-compatible chat endpoint, SSE streaming ]
        |
 Authentication and virtual keys (per team, app, user)
        |
 Rate limiter (requests per minute and tokens per minute)
        |
 Budget check (daily and monthly spend caps per key)
        |
 Guardrails hook (PII, jailbreak checks)
        |
 Semantic cache (hit: return immediately)
        |
 Router (policy: cost, latency, health, task complexity)
        |
 Provider adapters: Azure OpenAI | AWS Bedrock | Ollama (local)
   with retries, timeouts, circuit breaker and fallback chain
        |
 Telemetry: OpenTelemetry spans and metrics to Langfuse / dashboards
```

Optional stretch layer: an **MCP proxy** that applies the same authentication, limits and audit logging to tool calls.

## 5. Components

### 5.1 Gateway API
- Single OpenAI-compatible entry point so existing clients need no changes
- Streaming responses over SSE
- **Open design question:** if a stream fails partway through, should the gateway restart on another provider, try to resume, or surface the error? The choice and its trade-offs will be documented.

### 5.2 Virtual keys and budgets
- Keys map to a team, app or user, never to raw provider credentials
- Each key carries limits, a budget and an allowed model list

### 5.3 Rate limiting
- Two limits per key: requests per minute and tokens per minute
- Token cost is estimated before the call, then reconciled with actual usage afterward
- Shared state in Redis so limits hold across multiple gateway instances

### 5.4 Router and fallback
- Routes defined by policy files, not hardcoded
- Each route has an ordered provider chain, timeouts, retry conditions and circuit-breaker settings
- Health tracking so unhealthy providers are skipped quickly
- Last-resort local model, clearly marked as degraded quality

### 5.5 Semantic cache
- Prompts are embedded and matched against earlier ones above a similarity threshold
- Safety rules: no cross-tenant cache sharing, skip caching for prompts with user-specific data, short expiry where freshness matters
- The false-hit rate is logged and reported next to the hit rate

### 5.6 Guardrails hook
- Pluggable pre- and post-call checks (PII redaction, jailbreak detection, output validation)
- Reuses existing guardrail experience rather than inventing new checks

### 5.7 Observability and cost attribution
- A trace span for each hop: inference, cache lookup, retries, fallbacks
- Attributes on each span: provider, model, token counts, cost, cache hit, fallback reason
- Cost aggregated by team and app, with spend, cached savings and cost per successful request
- Follows OpenTelemetry GenAI conventions with the version pinned in the repository

## 6. Differentiator: quality-aware routing

Cost-based routing is common. The more interesting version asks whether the cheap model was good enough.

Idea: send simple requests to a cheaper model, check the answer with a lightweight quality signal (an LLM-as-judge or a confidence check), and escalate to a stronger model only when the check fails. The gateway then reports the cost saved and the quality retained.

This ties directly to existing evaluation experience and is the part least likely to look like a copy of an existing gateway. The exact quality signal is still to be decided.

## 7. Evaluation plan

Each claim in the final write-up needs a test behind it.

| Area | Test | Metric to report |
|---|---|---|
| Failover | Kill or throttle the primary provider under load | Request success rate during failure, time to recover |
| Streaming | Break a stream midway | Behaviour per the chosen strategy |
| Rate limiting | Burst traffic from several keys | Correct rejections, no cross-key leakage |
| Caching | Replay a realistic mixed workload | Hit rate, false-hit rate, cost saved |
| Overhead | Load test with and without the gateway | p50 and p95 added latency at a stated request rate |
| Routing | Mixed easy and hard prompts | Cost per request and quality score vs always-strong baseline |
| Observability | Inspect traces for a failed-over request | Full path visible, cost attributed |

All target values are blank until measured.

## 8. Benchmark plan

Run the same workload against this gateway and against LiteLLM, on the same machine, with the same providers. Report overhead, failover behaviour and feature gaps, including where this project loses. A fair comparison is more credible than a flattering one.

## 9. Proposed stack

FastAPI, Redis, PostgreSQL with pgvector, OpenTelemetry, Langfuse, Docker. Providers: Azure OpenAI, AWS Bedrock, Ollama. Stack choices are proposals and can change.

## 10. Roadmap

| Phase | Scope |
|---|---|
| 1 | OpenAI-compatible endpoint, two providers, streaming |
| 2 | Fallback chain, circuit breaker, chaos tests |
| 3 | Redis rate limiting, virtual keys, budgets |
| 4 | OpenTelemetry tracing, Langfuse, cost dashboard |
| 5 | Semantic cache with false-hit measurement |
| 6 | Quality-aware routing |
| 7 | Load tests and comparison against LiteLLM |
| 8 (stretch) | MCP proxy with per-key tool permissions |

Rough estimate for phases 1 to 7: three to four weekends. This is a guess, not a commitment.

## 11. Risks and open decisions

- **Streaming failover behaviour** is undecided and is the hardest design choice
- **Cache correctness:** a wrong cache hit is worse than a miss, so thresholds need real tuning
- **Quality signal for routing** needs a cheap, reliable check
- **OpenTelemetry conventions** may change, so versions must be pinned
- **Scope creep:** the MCP layer and extra providers are easy to over-invest in
- **Provider access and cost:** testing against real providers costs money, so use small workloads and the local model where possible

## 12. Definition of done

- Demonstrated failover under injected failure
- Published load-test results with the method described
- Comparison table against LiteLLM, including weaknesses
- README, architecture diagram and a short write-up of design decisions
- Every metric on the resume traceable to a run in this repository

## 13. Sources

- 5 Best LLM Gateways for AI Agents in 2026: https://getmaxim.ai/articles/5-best-llm-gateways-for-ai-agents-in-2026-mcp-support-tool-governance-and-cost-tracking/
- Best 7 AI Gateways for Agentic AI in 2026: https://futureagi.com/blog/best-ai-gateways-agentic-ai/
- AI Gateways vs LLM Gateways in 2026: https://futureagi.com/blog/best-ai-gateways-vs-llm-gateways-2026/
- Top 5 MCP Gateways in 2026: https://getmaxim.ai/articles/top-5-mcp-gateways-in-2026-4
- OpenTelemetry GenAI Semantic Conventions Explained (Dash0): https://www.dash0.com/knowledge/opentelemetry-genai-semantic-conventions-explained
- Top 5 Open-Source LLM Gateways Compared (2026): https://getmaxim.ai/articles/top-5-open-source-llm-gateways-compared-2026/
- 6 Best Open-Source OpenRouter Alternatives in 2026: https://llmgateway.io/blog/open-source-openrouter-alternatives
- AI Gateway Comparison, Portkey vs LiteLLM vs Kong vs Cloudflare: https://www.tooljunction.io/blog/?p=3949
- LLM Gateways as Cost Control Centers: https://www.tkmxai.it.com/llm-gateways-as-cost-control-centers
- 5 Best Practices to Optimize Your LLM Costs in Production: https://www.getmaxim.ai/articles/5-best-practices-to-optimize-your-llm-costs-in-production/
- How to Reduce LLM Costs with an AI Gateway (NeuralTrust): https://neuraltrust.ai/blog/ai-gateway-llm-cost-optimization
