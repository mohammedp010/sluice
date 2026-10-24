// Turns a trace's raw spans into what the pipeline map and inspector show: one verdict per
// gateway stage, the upstream attempts in order, and the events that explain hand-offs.
// Every fact here is read from span names, attributes and events the gateway writes
// (src/sluice/api/app.py, src/sluice/router/executor.py); nothing is inferred from timing.

import type { Attr, Span, SpanEvent } from "./api";

export type StageId = "client" | "auth" | "limits" | "cache" | "executor" | "quality" | "response";
export type Verdict = "pass" | "fail" | "hit" | "skip" | "warn" | "idle";

export interface Stage {
  id: StageId;
  verdict: Verdict;
  note: string;
  span?: Span;
}

export interface AttemptView {
  span: Span;
  provider: string;
  model: string;
  ok: boolean;
  errorType: string | null;
  index: number; // retry number on this target (sluice.attempt)
  escalation: boolean; // made on the escalate route
}

export interface TraceView {
  root: Span;
  t0: number;
  t1: number;
  status: number;
  outcome: string;
  provider: string;
  stages: Stage[];
  attempts: AttemptView[];
  fallbacks: SpanEvent[];
  escalate: SpanEvent | null;
  resume: SpanEvent | null;
  depth: Map<string, number>;
  spans: Span[];
}

export const STAGE_LABEL: Record<StageId, string> = {
  client: "Client",
  auth: "Auth",
  limits: "Limits",
  cache: "Cache",
  executor: "Executor",
  quality: "Quality",
  response: "Response",
};

const s = (a: Attr | undefined) => (a === undefined ? "" : String(a));

/** `routes` (from the admin overview) lets the quality stage say "no checks" for routes that
 * have no escalate route, instead of claiming checks passed. */
export function analyse(spans: Span[], routes?: Record<string, { escalate: unknown }>): TraceView | null {
  if (!spans.length) return null;
  const ids = new Set(spans.map((x) => x.span_id));
  const root = spans.find((x) => !x.parent_id || !ids.has(x.parent_id));
  if (!root) return null;

  const depth = new Map<string, number>();
  const byId = new Map(spans.map((x) => [x.span_id, x]));
  const d = (x: Span): number => {
    if (depth.has(x.span_id)) return depth.get(x.span_id)!;
    const p = x.parent_id ? byId.get(x.parent_id) : undefined;
    const v = p ? d(p) + 1 : 0;
    depth.set(x.span_id, v);
    return v;
  };
  spans.forEach(d);

  const A = root.attributes;
  const status = Number(A["http.response.status_code"] ?? 0);
  const outcome = s(A["sluice.outcome"]) || "unknown";
  const provider = s(A["sluice.provider"]);
  const named = (n: string) => spans.find((x) => x.name === n);
  const auth = named("sluice.auth"), limits = named("sluice.limits"), cache = named("sluice.cache");

  const escalate = root.events.find((e) => e.name === "sluice.escalate") ?? null;
  const escalateAt = escalate?.ts_ns ?? Infinity;
  const attempts: AttemptView[] = spans
    .filter((x) => x.kind === "client" && x.span_id !== root.span_id)
    .sort((a, b) => a.start_ns - b.start_ns)
    .map((x) => ({
      span: x,
      provider: s(x.attributes["gen_ai.provider.name"]),
      model: s(x.attributes["gen_ai.request.model"]),
      ok: x.status !== "error",
      errorType: x.attributes["error.type"] !== undefined ? s(x.attributes["error.type"]) : null,
      index: Number(x.attributes["sluice.attempt"] ?? 0),
      escalation: x.start_ns > escalateAt,
    }));
  const fallbacks = root.events.filter((e) => e.name === "sluice.fallback");
  const resume = root.events.find((e) => e.name === "sluice.stream_resume") ?? null;

  // Notes are what the map prints under each stage: kept to ~10 characters so they fit.
  const stages: Stage[] = [{ id: "client", verdict: "pass", note: A["sluice.stream"] ? "streamed" : "request" }];
  const authFail = ["invalid_api_key", "model_not_found", "model_not_allowed"].includes(outcome);
  stages.push({
    id: "auth", span: auth,
    verdict: authFail ? "fail" : auth ? "pass" : "idle",
    note: authFail ? { invalid_api_key: "bad key", model_not_found: "no route", model_not_allowed: "denied" }[outcome]!
      : s(A["sluice.team"]) || "key ok",
  });
  const limitFail = ["rate_limit_exceeded", "budget_exceeded", "limiter_unavailable"].includes(outcome);
  stages.push({
    id: "limits", span: limits,
    verdict: authFail ? "idle" : limitFail ? "fail" : limits ? "pass" : "idle",
    note: limitFail ? { rate_limit_exceeded: "429 rate", budget_exceeded: "budget", limiter_unavailable: "store down" }[outcome]!
      : limits ? `~${s(limits.attributes["sluice.tokens.estimate"])} tok` : "",
  });
  const cacheResult = cache ? s(cache.attributes["sluice.cache.result"]) : "";
  const hit = cacheResult === "hit";
  const sim = cache?.attributes["sluice.cache.similarity"];
  stages.push({
    id: "cache", span: cache,
    verdict: !cache ? "idle" : hit ? "hit" : cacheResult === "skip" ? "skip" : "pass",
    note: !cache ? "" : hit ? `hit ${Number(sim).toFixed(3)}`
      : cacheResult === "skip" ? (s(cache.attributes["sluice.cache.reason"]) === "disabled" ? "off" : `skip`)
      : "miss",
  });
  const execFail = ["all_providers_failed", "upstream_rejected", "stream_interrupted"].includes(outcome);
  const retried = attempts.length > 1 || fallbacks.length > 0;
  stages.push({
    id: "executor",
    verdict: !cache || hit ? (hit ? "skip" : "idle") : execFail ? "fail" : retried || resume ? "warn" : "pass",
    note: hit ? "not called"
      : !attempts.length ? "" : execFail ? outcome.replaceAll("_", " ")
      : resume ? `resumed on ${s(resume.attributes["sluice.next_provider"])}`
      : `${attempts.length} attempt${attempts.length > 1 ? "s" : ""} → ${provider}`,
  });
  const route = routes?.[s(A["gen_ai.request.model"])];
  const checked = !route || (!!route.escalate && !A["sluice.stream"]); // streams skip checks (ADR 0006)
  stages.push({
    id: "quality",
    verdict: escalate ? "warn" : hit || execFail || !attempts.length ? "idle" : checked ? "pass" : "skip",
    note: escalate ? "escalated" : !attempts.length || execFail || hit ? "" : checked ? "passed" : "no checks",
  });
  stages.push({
    id: "response",
    verdict: status >= 400 || outcome === "stream_interrupted" ? "fail" : status ? "pass" : "idle",
    note: `${status || "—"} ${outcome === "ok" ? "ok" : outcome.split("_")[0]}`,
  });

  const t0 = Math.min(...spans.map((x) => x.start_ns));
  const t1 = Math.max(...spans.map((x) => x.end_ns));
  return { root, t0, t1, status, outcome, provider, stages, attempts, fallbacks, escalate, resume, depth, spans };
}

/** Spans in display order: depth-first from the root, children by start time. */
export function ordered(view: TraceView): Span[] {
  const kids = new Map<string, Span[]>();
  for (const x of view.spans) {
    if (x === view.root) continue;
    const k = x.parent_id ?? "";
    kids.set(k, [...(kids.get(k) ?? []), x]);
  }
  const out: Span[] = [];
  const walk = (x: Span) => {
    out.push(x);
    for (const c of (kids.get(x.span_id) ?? []).sort((a, b) => a.start_ns - b.start_ns)) walk(c);
  };
  walk(view.root);
  return out;
}

/** Which pipeline stage a span belongs to, for linked hover between waterfall and map. */
export function stageOf(span: Span, view: TraceView): StageId {
  if (span === view.root) return "response";
  if (span.name === "sluice.auth") return "auth";
  if (span.name === "sluice.limits") return "limits";
  if (span.name === "sluice.cache") return "cache";
  if (span.kind === "client") return "executor";
  return "response";
}
