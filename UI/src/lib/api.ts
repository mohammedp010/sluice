// Typed client for the gateway: the OpenAI-compatible endpoint and the read-only admin API
// (src/sluice/api/admin.py). The Vite dev server proxies every path below to the gateway.

export type BreakerState = "closed" | "half_open" | "open";

export interface Target {
  provider: string;
  upstream_model: string;
  timeout_s: number;
  retries: number;
  price_in_per_mtok: number;
  price_out_per_mtok: number;
}

export interface Route {
  chain: Target[];
  deadline_s: number;
  cache: boolean;
  escalate: Route | null;
  stream_resume: "off" | "prefill" | "instruct";
  escalate_on: string[];
}

export interface Provider {
  type: "mock" | "openai_compat";
  text: string | null;
  latency_ms: number;
  tokens_per_sec: number | null;
  error_rate: number;
  error_status: number;
  cut_stream_after: number | null;
  base_url: string | null;
  timeout_s: number;
  paid: boolean;
  has_api_key: boolean;
  breaker: { state: BreakerState; failures: number; threshold: number; cooldown_s: number } | null;
}

export interface Overview {
  routes: Record<string, Route>;
  providers: Record<string, Provider>;
  auth: { enabled: boolean; max_body_bytes: number };
  state: { backend: "memory" | "redis" };
  limits: { default_completion_estimate: number; default_max_tokens: number | null; paid_daily_call_cap: number };
  paid_allowed: boolean;
  cache: {
    enabled: boolean; embed_model: string; embed_prefix: string; threshold: number;
    guard_max_diff: number | null; ttl_s: number; max_entries: number; max_temperature: number;
  };
  telemetry: { service_name: string; traces: string; prometheus: boolean; ledger: boolean };
  mcp: { servers: Record<string, "stdio" | "http">; allowed_origins: string[] };
  recent_traces: number;
}

export type Attr = string | number | boolean | Array<string | number | boolean>;

export interface SpanEvent {
  name: string;
  ts_ns: number;
  attributes: Record<string, Attr>;
}

export interface Span {
  span_id: string;
  parent_id: string | null;
  name: string;
  kind: "server" | "client" | "internal" | "producer" | "consumer";
  start_ns: number;
  end_ns: number;
  status: "unset" | "ok" | "error";
  attributes: Record<string, Attr>;
  events: SpanEvent[];
}

export interface TraceRoot extends Span {
  trace_id: string;
  span_count: number;
}

export interface LedgerRow {
  id: number;
  ts: string;
  trace_id: string;
  key_id: number;
  key_name: string;
  team: string;
  app: string;
  model: string;
  provider: string;
  upstream_model: string | null;
  status: number;
  outcome: string;
  attempts: number;
  fallbacks: number;
  prompt_tokens: number;
  completion_tokens: number;
  cost_micro_usd: number;
  latency_ms: number;
}

export interface Trace {
  trace_id: string;
  spans: Span[];
  ledger: LedgerRow | null;
}

export interface SummaryRow {
  group: string;
  requests: number;
  errors: number;
  fallbacks: number;
  prompt_tokens: number;
  completion_tokens: number;
  cost_usd: number;
}

export interface KeyInfo {
  id: number;
  name: string;
  team: string;
  app: string;
  hint: string;
  allowed_models: string[];
  allowed_tools: string[];
  rpm: number;
  tpm: number;
  daily_budget_usd: number | null;
  monthly_budget_usd: number | null;
  revoked: boolean;
  status: "active" | "suspended" | "revoked";
  suspended_reason: string | null;
  expires_at: string | null; // UTC, "YYYY-MM-DD HH:MM:SS"
  allowed_cidrs: string[];
  lineage: number;
  created_at: string;
  spend_today_usd: number;
  spend_month_usd: number;
}

export interface AuditRow {
  id: number;
  ts: string;
  trace_id: string | null;
  key_id: number;
  key_name: string;
  team: string;
  tool: string;
  outcome: string;
  args_sha256: string | null;
  args_bytes: number | null;
  latency_ms: number | null;
  error: string | null;
}

export interface McpInfo {
  servers: Record<string, "stdio" | "http">;
  summary: { key: string; tool: string; outcome: string; calls: number }[];
  audit: AuditRow[];
  pins: PinRow[];
}

/** A tool's pin (ADR 0011): the trusted definition's hash against the one seen last. */
export interface PinRow {
  tool: string;
  status: "approved" | "pending" | "quarantined";
  pinned_sha256: string;
  seen_sha256: string;
  flags: string; // JSON list of lint findings
  first_seen: string;
  changed_at: string | null;
  approved_at: string | null;
}

/** Why a read failed, in terms the UI can explain. */
export class ApiError extends Error {
  constructor(
    message: string,
    readonly kind: "offline" | "admin_off" | "http",
    readonly status?: number,
  ) {
    super(message);
  }
}

async function get<T>(path: string): Promise<T> {
  let r: Response;
  try {
    r = await fetch(path);
  } catch {
    throw new ApiError("Gateway unreachable", "offline");
  }
  // the dev proxy answers 500/502/504 with an empty body when nothing listens on :8000
  if (r.status >= 500 && !(r.headers.get("content-type") ?? "").includes("json")) {
    throw new ApiError("Gateway unreachable", "offline", r.status);
  }
  if (r.status === 404 && path.startsWith("/v1/sluice/admin")) {
    throw new ApiError("Admin API is off", "admin_off", 404);
  }
  if (!r.ok) throw new ApiError(`${r.status} ${r.statusText}`, "http", r.status);
  return r.json() as Promise<T>;
}

export const api = {
  overview: () => get<Overview>("/v1/sluice/admin/overview"),
  health: () => get<Record<string, BreakerState>>("/v1/sluice/health"),
  traces: (limit = 60) => get<TraceRoot[]>(`/v1/sluice/admin/traces?limit=${limit}`),
  trace: (id: string) => get<Trace>(`/v1/sluice/admin/traces/${id}`),
  ledger: (limit = 200) => get<LedgerRow[]>(`/v1/sluice/admin/ledger?limit=${limit}`),
  summary: (by: string, since?: string) =>
    get<SummaryRow[]>(`/v1/sluice/admin/ledger/summary?by=${by}${since ? `&since=${since}` : ""}`),
  keys: () => get<KeyInfo[]>("/v1/sluice/admin/keys"),
  mcp: () => get<McpInfo>("/v1/sluice/admin/mcp"),
};

// ---------------------------------------------------------------------------------------------
// Sending a chat completion, streamed or not, and keeping everything the response says.

export interface SendOptions {
  key: string;
  model: string;
  prompt: string;
  stream: boolean;
  noCache: boolean;
  json: boolean;
  maxTokens: number | null;
}

export interface SendResult {
  status: number;
  headers: Record<string, string>;
  traceId: string | null;
  content: string;
  finishReason: string | null;
  error: { message: string; code: string } | null;
  /** streams only: true when [DONE] arrived */
  done: boolean | null;
  ms: number;
}

export async function send(o: SendOptions, onDelta: (text: string) => void): Promise<SendResult> {
  const t0 = performance.now();
  const body: Record<string, unknown> = {
    model: o.model,
    messages: [{ role: "user", content: o.prompt }],
    stream: o.stream,
  };
  if (o.maxTokens) body.max_tokens = o.maxTokens;
  if (o.json) body.response_format = { type: "json_object" };
  const headers: Record<string, string> = { "content-type": "application/json" };
  if (o.key) headers.authorization = `Bearer ${o.key}`;
  if (o.noCache) headers["cache-control"] = "no-cache";

  let r: Response;
  try {
    r = await fetch("/v1/chat/completions", { method: "POST", headers, body: JSON.stringify(body) });
  } catch {
    throw new ApiError("Gateway unreachable", "offline");
  }
  const h: Record<string, string> = {};
  r.headers.forEach((v, k) => (h[k] = v));
  const base = { status: r.status, headers: h, traceId: h["x-sluice-trace-id"] ?? null };

  const isStream = (h["content-type"] ?? "").includes("text/event-stream");
  if (!isStream) {
    const data = await r.json().catch(() => null);
    if (!r.ok || !data?.choices) {
      return {
        ...base, content: "", finishReason: null, done: null, ms: performance.now() - t0,
        error: { message: data?.error?.message ?? r.statusText, code: data?.error?.code ?? String(r.status) },
      };
    }
    const content = data.choices[0].message.content ?? "";
    onDelta(content);
    return { ...base, content, finishReason: data.choices[0].finish_reason, error: null, done: null, ms: performance.now() - t0 };
  }

  // SSE: `data: {...}\n\n` frames, `data: [DONE]` at a clean end, an error frame otherwise.
  const reader = r.body!.getReader();
  const dec = new TextDecoder();
  let buf = "", content = "", finishReason: string | null = null, done = false;
  let error: SendResult["error"] = null;
  for (;;) {
    const { value, done: eof } = await reader.read();
    if (eof) break;
    buf += dec.decode(value, { stream: true });
    let i: number;
    while ((i = buf.indexOf("\n\n")) >= 0) {
      const frame = buf.slice(0, i).trim();
      buf = buf.slice(i + 2);
      if (!frame.startsWith("data:")) continue;
      const payload = frame.slice(5).trim();
      if (payload === "[DONE]") { done = true; continue; }
      const obj = JSON.parse(payload);
      if (obj.error) { error = { message: obj.error.message, code: obj.error.code }; continue; }
      const choice = obj.choices?.[0];
      if (choice?.delta?.content) {
        content += choice.delta.content;
        onDelta(content);
      }
      if (choice?.finish_reason) finishReason = choice.finish_reason;
    }
  }
  return { ...base, content, finishReason, error, done, ms: performance.now() - t0 };
}
