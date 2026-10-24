import { useState } from "react";
import { ArrowDown, BookOpen, Check, Database, RotateCw, Scissors, X, Zap } from "lucide-react";
import type { LedgerRow, SendResult, Span } from "../lib/api";
import { int, ms, nsToMs, usd } from "../lib/format";
import type { TraceView } from "../lib/trace";
import { go } from "../lib/nav";
import { Empty } from "./state";

type Tab = "response" | "trail" | "headers" | "cost" | "span";

export interface InspectorProps {
  view: TraceView | null;
  ledger: LedgerRow | null;
  local: { result: SendResult | null; text: string; pending: boolean } | null;
  span: Span | null;
}

export default function Inspector({ view, ledger, local, span }: InspectorProps) {
  const [tab, setTab] = useState<Tab>("trail");
  const tabs: [Tab, string][] = [["trail", "Trail"], ["response", "Response"], ["headers", "Headers"], ["cost", "Cost"], ["span", "Span"]];
  return (
    <>
      <div className="subtabs" role="tablist" aria-label="Inspector">
        {tabs.map(([id, label]) => (
          <button key={id} role="tab" aria-selected={tab === id} onClick={() => setTab(id)}>
            {label}
          </button>
        ))}
      </div>
      <div className="pane-b" role="tabpanel">
        {tab === "response" && <ResponseTab local={local} />}
        {tab === "headers" && <HeadersTab local={local} />}
        {tab === "trail" && <TrailTab view={view} ledger={ledger} local={local} />}
        {tab === "cost" && <CostTab ledger={ledger} />}
        {tab === "span" && <SpanTab span={span ?? view?.root ?? null} />}
      </div>
    </>
  );
}

const notFromHere = (
  <Empty title="Sent before this page opened">
    The gateway keeps each request's spans and its ledger row, but not the response body or headers.
    Send one from the composer to see them here.
  </Empty>
);

function ResponseTab({ local }: Pick<InspectorProps, "local">) {
  if (!local) return notFromHere;
  const r = local.result;
  return (
    <>
      {r?.error && (
        <div style={{ padding: 10 }}>
          <div className="notice bad">
            <X aria-hidden />
            <div>
              <h3 className="mono">{r.error.code}</h3>
              <p>{r.error.message}</p>
              {r.done === false && <p>The stream ended with an error event and no <code>[DONE]</code>: that is ADR 0001's rule for a failure after the first byte.</p>}
            </div>
          </div>
        </div>
      )}
      <div className={`response ${local.text ? "" : "empty-text"}`} aria-live="polite">
        {local.text || (local.pending ? "" : "(empty answer)")}
        {local.pending && <span className="caret" aria-hidden />}
      </div>
      {r && (
        <div className="kv" style={{ borderTop: "1px solid var(--line)" }}>
          <div>finish_reason</div><div>{r.finishReason ?? "—"}</div>
          {r.done !== null && (<><div>[DONE]</div><div>{r.done ? "received" : "missing"}</div></>)}
          <div>client time</div><div>{ms(r.ms)}</div>
        </div>
      )}
    </>
  );
}

function HeadersTab({ local }: Pick<InspectorProps, "local">) {
  if (!local) return notFromHere;
  if (!local.result) return <Empty title="Waiting for the response headers" />;
  const h = Object.entries(local.result.headers);
  const ours = h.filter(([k]) => k.startsWith("x-sluice")).sort();
  const rest = h.filter(([k]) => !k.startsWith("x-sluice")).sort();
  return (
    <>
      <div className="kv-h">HTTP {local.result.status}</div>
      <div className="kv">
        {ours.map(([k, v]) => (<Pair key={k} k={k} v={v} em />))}
        {rest.map(([k, v]) => (<Pair key={k} k={k} v={v} />))}
      </div>
    </>
  );
}

const Pair = ({ k, v, em }: { k: string; v: string; em?: boolean }) => (
  <>
    <div className={em ? "em" : ""}>{k}</div>
    <div>{v}</div>
  </>
);

function Adr({ n }: { n: string }) {
  return (
    <a href={`#decisions/${n}`} onClick={(e) => { e.preventDefault(); go("decisions", n); }} style={{ display: "inline-flex", alignItems: "center", gap: 3 }}>
      <BookOpen width={11} height={11} aria-hidden />ADR {n}
    </a>
  );
}

function TrailTab({ view, ledger, local }: Pick<InspectorProps, "view" | "ledger" | "local">) {
  if (!view) {
    if (local?.pending) return <Empty title="Request in flight">The trail appears when the gateway closes the request span.</Empty>;
    return <Empty title="Nothing selected">Send a request, or pick one from the list on the left.</Empty>;
  }
  const A = view.root.attributes;
  const t = (ns: number) => ms(nsToMs(ns - view.t0));
  const items: { icon: JSX.Element; what: JSX.Element | string; why?: JSX.Element | string; ns?: number }[] = [];
  const cache = view.stages.find((s) => s.id === "cache")!;
  const auth = view.stages.find((s) => s.id === "auth")!;
  const limits = view.stages.find((s) => s.id === "limits")!;

  items.push({ icon: auth.verdict === "fail" ? <X className="v-fail" /> : <Check className="v-pass" />, what: auth.verdict === "fail" ? `rejected: ${auth.note}` : `key ${ledger?.key_name ?? A["sluice.key.id"] ?? ""} · ${auth.note}`, ns: auth.span?.start_ns });
  if (limits.verdict !== "idle") items.push({ icon: limits.verdict === "fail" ? <X className="v-fail" /> : <Check className="v-pass" />, what: `limits: ${limits.note}`, why: limits.verdict === "fail" ? <>Checked before any upstream call. <Adr n="0003" /></> : undefined, ns: limits.span?.start_ns });
  if (cache.verdict !== "idle") items.push({ icon: cache.verdict === "hit" ? <Zap className="v-hit" /> : <Database className="v-skip" />, what: `cache ${cache.note}`, why: cache.verdict === "hit" ? <>Answered without calling a provider. <Adr n="0005" /></> : undefined, ns: cache.span?.start_ns });

  view.attempts.forEach((a) => {
    items.push({
      icon: a.ok ? <Check className="v-pass" /> : <X className="v-fail" />,
      what: `${a.provider} · ${a.model} · try ${a.index + 1}${a.escalation ? " (escalated)" : ""}`,
      why: a.ok ? `${ms(nsToMs(a.span.end_ns - a.span.start_ns))}` : `${a.errorType ?? "error"} after ${ms(nsToMs(a.span.end_ns - a.span.start_ns))}`,
      ns: a.span.start_ns,
    });
    const fb = view.fallbacks.find((e) => e.attributes["sluice.provider"] === a.provider && e.ts_ns >= a.span.end_ns - 1e6 && !a.ok);
    if (fb && view.attempts.filter((x) => x.provider === a.provider).at(-1) === a) {
      const next = fb.attributes["sluice.next_provider"];
      items.push({
        icon: <ArrowDown className="v-warn" />,
        what: next ? `fell over: ${a.provider} → ${next}` : `gave up on ${a.provider}: last target`,
        why: <>reason {String(fb.attributes["error.type"])}. <Adr n="0002" /></>,
        ns: fb.ts_ns,
      });
    }
  });
  if (view.escalate) {
    items.push({
      icon: <RotateCw className="v-warn" />,
      what: `escalated: failed ${String(view.escalate.attributes["sluice.checks"])}`,
      why: <>The cheap answer failed a free check and was re-asked on the stronger route; both are billed. <Adr n="0006" /></>,
      ns: view.escalate.ts_ns,
    });
  }
  if (view.resume) {
    const r = view.resume.attributes;
    items.push({
      icon: <Scissors className="v-warn" />,
      what: `stream cut on ${String(r["sluice.provider"])}, resumed on ${String(r["sluice.next_provider"])}`,
      why: <>{String(r["sluice.resume.prefix_chars"])} chars already sent were given to the backup ({String(r["sluice.resume.strategy"])}), and repeated text at the seam was cut. <Adr n="0008" /></>,
      ns: view.resume.ts_ns,
    });
  }
  if (view.outcome === "stream_interrupted") {
    items.push({ icon: <X className="v-fail" />, what: "stream interrupted after the first byte", why: <>Terminal error event, no [DONE]. <Adr n="0001" /></>, ns: view.root.end_ns - 1 });
  }
  items.push({
    icon: view.status >= 400 ? <X className="v-fail" /> : <Check className="v-pass" />,
    what: `${view.status} ${view.outcome.replaceAll("_", " ")}${view.provider && view.provider !== "none" ? ` · served by ${view.provider}` : ""}`,
    ns: view.root.end_ns,
  });

  return (
    <>
      <div className="summary-strip">
        <span className="pill neutral">{ms(nsToMs(view.t1 - view.t0))}</span>
        <span className="pill neutral">{view.attempts.length} attempt{view.attempts.length === 1 ? "" : "s"}</span>
        {view.fallbacks.length > 0 && <span className="pill warn">{view.fallbacks.length} fallback{view.fallbacks.length > 1 ? "s" : ""}</span>}
        {ledger && <span className="pill accent">{usd(ledger.cost_micro_usd / 1e6)}</span>}
      </div>
      <ol className="trail">
        {items.map((it, i) => ({ ...it, i })).sort((a, b) => (a.ns ?? 0) - (b.ns ?? 0) || a.i - b.i).map((it, i) => (
          <li key={i}>
            {it.icon}
            <span className="what">{it.what}</span>
            <span className="t">{it.ns !== undefined ? t(it.ns) : ""}</span>
            {it.why && <span className="why">{it.why}</span>}
          </li>
        ))}
      </ol>
    </>
  );
}

function CostTab({ ledger }: Pick<InspectorProps, "ledger">) {
  if (!ledger) {
    return (
      <Empty title="No ledger row for this trace">
        Only requests that reached a provider (or the cache) are billed. Rejected ones, at auth or limits, cost nothing.
      </Empty>
    );
  }
  const L = ledger;
  return (
    <>
      <div className="kv-h">
        {usd(L.cost_micro_usd / 1e6)} billed to team <b>{L.team || "—"}</b>, app <b>{L.app || "—"}</b>
      </div>
      <div className="kv">
        <div>key</div><div>{L.key_name} (#{L.key_id})</div>
        <div>model → upstream</div><div>{L.model} → {L.upstream_model ?? "—"}</div>
        <div>provider</div><div>{L.provider}</div>
        <div>tokens in / out</div><div>{int(L.prompt_tokens)} / {int(L.completion_tokens)}</div>
        <div>cost_micro_usd</div><div>{int(L.cost_micro_usd)}</div>
        <div>attempts · fallbacks</div><div>{L.attempts} · {L.fallbacks}</div>
        <div>status · outcome</div><div>{L.status} · {L.outcome}</div>
        <div>latency</div><div>{ms(L.latency_ms)}</div>
        <div>ts</div><div>{L.ts}</div>
        <div>trace_id</div><div>{L.trace_id}</div>
      </div>
      <p style={{ padding: "10px", fontSize: "var(--fs-xs)", color: "var(--ink-2)" }}>
        Prices come from the route's fake price table, so budgets are exercised for $0. The same row,
        from a shell: <code>python -m sluice.report --trace {L.trace_id.slice(0, 8)}…</code>
      </p>
    </>
  );
}

function SpanTab({ span }: { span: Span | null }) {
  if (!span) return <Empty title="Pick a span">Click a row in the waterfall or a box on the map.</Empty>;
  const attrs = Object.entries(span.attributes);
  return (
    <>
      <div className="kv-h mono">{span.name}</div>
      <div className="kv">
        <div>kind · status</div><div>{span.kind} · {span.status}</div>
        <div>duration</div><div>{ms(nsToMs(span.end_ns - span.start_ns))}</div>
        <div>span_id</div><div>{span.span_id}</div>
        {attrs.map(([k, v]) => (
          <Pair key={k} k={k} v={Array.isArray(v) ? v.join(", ") : String(v)} em={k.startsWith("sluice.")} />
        ))}
      </div>
      {span.events.length > 0 && (
        <>
          <div className="kv-h">Events</div>
          <div className="kv">
            {span.events.map((e, i) => (
              <Pair key={i} k={e.name} v={JSON.stringify(e.attributes)} />
            ))}
          </div>
        </>
      )}
    </>
  );
}
