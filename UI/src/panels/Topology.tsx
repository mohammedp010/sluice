import { ArrowRight, BookOpen, Database, Gauge, KeyRound, Layers, Radio, ShieldCheck } from "lucide-react";
import type { ApiError, Overview, Route, Target } from "../lib/api";
import { go } from "../lib/nav";
import { providerBehaviour } from "../lib/routes";
import { BreakerMark } from "../components/marks";
import { ApiProblem, SkeletonRows } from "../components/state";

const Adr = ({ n }: { n: string }) => (
  <a href={`#decisions/${n}`} onClick={(e) => { e.preventDefault(); go("decisions", n); }} style={{ fontSize: "var(--fs-xs)", display: "inline-flex", gap: 3, alignItems: "center" }}>
    <BookOpen width={11} height={11} aria-hidden />ADR {n}
  </a>
);

function Tgt({ t, o }: { t: Target; o: Overview }) {
  const p = o.providers[t.provider];
  const priced = t.price_in_per_mtok || t.price_out_per_mtok;
  return (
    <div className="tgt">
      <span className="p">{p?.breaker && <BreakerMark state={p.breaker.state} withWord={false} />}{t.provider}</span>
      <span className="d">{t.upstream_model}</span>
      <span className="d">timeout {t.timeout_s}s · retries {t.retries}</span>
      <span className="d">{priced ? `$${t.price_in_per_mtok} / $${t.price_out_per_mtok} per Mtok` : "free"}</span>
      <span className="d" style={{ color: p?.error_rate || p?.cut_stream_after != null || p?.text === "" ? "var(--warn)" : undefined }}>{providerBehaviour(p)}</span>
    </div>
  );
}

function Chain({ r, o }: { r: Route; o: Overview }) {
  return (
    <div className="chain">
      {r.chain.map((t, i) => (
        <div key={i} style={{ display: "flex" }}>
          {i > 0 && <span className="arrow" title="falls over to"><ArrowRight aria-hidden /></span>}
          <Tgt t={t} o={o} />
        </div>
      ))}
    </div>
  );
}

export default function Topology({ overview, focus }: { overview: { data: Overview | null; error: ApiError | null }; focus: string }) {
  const o = overview.data;
  if (!o) {
    return <div className="page"><div className="page-in">{overview.error ? <ApiProblem error={overview.error} /> : <SkeletonRows n={10} />}</div></div>;
  }
  const c = o.cache;
  return (
    <div className="page">
      <div className="page-in">
        <div className="page-head">
          <div>
            <h1>Topology</h1>
            <p>The policy the running gateway loaded: public model names (routes), the provider chain each one tries in order, and every limit, cache and telemetry setting. Breaker states are live.</p>
          </div>
        </div>

        <section className="section">
          <h2>Routes</h2>
          <p className="lead">Clients send a <code>model</code>, and the route decides where it goes. A target is retried <code>retries</code> times, then the next one is tried, all inside the route's deadline. <Adr n="0002" /></p>
          <div className="frame">
            {Object.entries(o.routes).map(([name, r]) => (
              <div key={name} className="route">
                <div className="route-h">
                  <h3>{name}</h3>
                  <span className="pill neutral">deadline {r.deadline_s}s</span>
                  {r.cache && <span className={`pill ${c.enabled ? "hit" : "neutral"}`}>cache {c.enabled ? "on" : "(globally off)"}</span>}
                  {r.stream_resume !== "off" && <span className="pill warn">stream resume: {r.stream_resume}</span>}
                  {r.escalate && <span className="pill accent">escalates</span>}
                </div>
                <Chain r={r} o={o} />
                {r.escalate && (
                  <div className="esc">
                    <span>re-asked here when a non-streamed answer fails: {r.escalate_on.join(", ")} <Adr n="0006" /></span>
                    <Chain r={r.escalate} o={o} />
                  </div>
                )}
              </div>
            ))}
          </div>
        </section>

        <section className="section">
          <h2>Providers</h2>
          <p className="lead">Health belongs to the provider, so each one has its own breaker: it opens after <i>threshold</i> consecutive failures and lets one probe through after the cooldown.</p>
          <div className="frame tblwrap">
            <table className="tbl">
              <thead><tr><th>provider</th><th>breaker</th><th className="num">failures</th><th className="num">cooldown</th><th>behaviour</th><th>paid</th></tr></thead>
              <tbody>
                {Object.entries(o.providers).map(([n, p]) => (
                  <tr key={n} aria-selected={focus === n}>
                    <td className="mono" style={{ fontWeight: 600 }}>{n}</td>
                    <td>{p.breaker ? <BreakerMark state={p.breaker.state} /> : "—"}</td>
                    <td className="num">{p.breaker ? `${p.breaker.failures} / ${p.breaker.threshold}` : "—"}</td>
                    <td className="num">{p.breaker ? `${p.breaker.cooldown_s}s` : "—"}</td>
                    <td className="mono">{providerBehaviour(p)}</td>
                    <td>{p.paid ? <span className="pill bad">paid{o.paid_allowed ? "" : " · blocked"}</span> : <span className="pill neutral">free</span>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>

        <section className="section">
          <h2>Gateway settings</h2>
          <div className="frame">
            <div className="cfg" style={{ borderTop: 0 }}>
              <div>
                <h3><KeyRound width={15} height={15} aria-hidden />Auth</h3>
                <p>{o.auth.enabled ? "Virtual keys required; only their SHA-256 is stored." : "Disabled: every caller is anonymous."}</p>
                <span className="num">body cap {(o.auth.max_body_bytes / 1e6).toFixed(1)} MB</span>
              </div>
              <div>
                <h3><Gauge width={15} height={15} aria-hidden />Limits <Adr n="0003" /></h3>
                <p>RPM and TPM token buckets per key; daily and monthly budgets, checked before every call.</p>
                <span className="num">state: {o.state.backend} · output estimate {o.limits.default_completion_estimate} tok</span>
                <span className="num">paid providers: {o.paid_allowed ? `allowed, ${o.limits.paid_daily_call_cap} calls/day` : "blocked (SLUICE_ALLOW_PAID unset)"}</span>
              </div>
              <div>
                <h3><Layers width={15} height={15} aria-hidden />Semantic cache <Adr n="0005" /></h3>
                <p>{c.enabled ? `${c.embed_model}, prefix "${c.embed_prefix}"` : "Off."}</p>
                {c.enabled && <span className="num">threshold {c.threshold} · guard ≤{c.guard_max_diff ?? "off"} words · ttl {c.ttl_s}s</span>}
                {c.enabled && (
                  <a href="#evidence/cache" onClick={(e) => { e.preventDefault(); go("evidence", "cache"); }} style={{ fontSize: "var(--fs-xs)" }}>
                    Why this threshold: 9% false hits on held-out pairs
                  </a>
                )}
              </div>
              <div>
                <h3><Radio width={15} height={15} aria-hidden />Telemetry <Adr n="0004" /></h3>
                <p>Traces: {o.telemetry.traces}{o.telemetry.traces === "none" ? " (kept in memory for this UI only)" : ""}.</p>
                <span className="num">/metrics {o.telemetry.prometheus ? "on" : "off"} · ledger {o.telemetry.ledger ? "on" : "off"} · last {o.recent_traces} traces held</span>
              </div>
              <div>
                <h3><ShieldCheck width={15} height={15} aria-hidden />MCP proxy <Adr n="0009" /></h3>
                <p>{Object.keys(o.mcp.servers).length ? Object.entries(o.mcp.servers).map(([n, k]) => `${n} (${k})`).join(", ") : "No tool servers configured."}</p>
                <span className="num">browser origins allowed: {o.mcp.allowed_origins.length || "none"}</span>
              </div>
              <div>
                <h3><Database width={15} height={15} aria-hidden />Stores</h3>
                <p>SQLite: keys, usage ledger, MCP audit. {o.state.backend === "redis" ? "Redis: buckets and spend, shared by replicas." : "Buckets and spend in memory (one replica)."}</p>
              </div>
            </div>
          </div>
        </section>
      </div>
    </div>
  );
}
