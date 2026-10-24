import { useState } from "react";
import { Receipt } from "lucide-react";
import { api } from "../lib/api";
import { ago, int, ms, usd } from "../lib/format";
import { go } from "../lib/nav";
import { StatusPill } from "../components/marks";
import { ApiProblem, Empty, SkeletonRows, usePoll } from "../components/state";

const GROUPS = ["team", "app", "key", "model", "provider", "day"] as const;

export default function Ledger() {
  const [by, setBy] = useState<(typeof GROUPS)[number]>("team");
  const summary = usePoll(() => api.summary(by), 5000, [by]);
  const rows = usePoll(() => api.ledger(200), 5000);
  const err = summary.error ?? rows.error;
  const s = summary.data ?? [];
  const maxCost = Math.max(...s.map((r) => r.cost_usd), 0);
  const tot = s.reduce((a, r) => ({ req: a.req + r.requests, err: a.err + r.errors, fb: a.fb + r.fallbacks, cost: a.cost + r.cost_usd }), { req: 0, err: 0, fb: 0, cost: 0 });

  return (
    <div className="page">
      <div className="page-in">
        <div className="page-head">
          <div>
            <h1>Ledger</h1>
            <p>One row per request that reached a provider or the cache, written off the request path. Team and app are copied onto each row when it's written, so spend stays with whoever owned the key at the time. Same data as <code>python -m sluice.report --by {by}</code>.</p>
          </div>
        </div>
        {err && !summary.data ? <ApiProblem error={err} /> : (
          <>
            <section className="section" style={{ marginTop: 0 }}>
              <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 10, flexWrap: "wrap" }}>
                <h2 style={{ fontSize: "var(--fs-lg)" }}>Spend by</h2>
                <div className="seg" role="group" aria-label="Group by">
                  {GROUPS.map((g) => <button key={g} aria-pressed={by === g} onClick={() => setBy(g)}>{g}</button>)}
                </div>
              </div>
              <div className="frame tblwrap">
                {summary.loading && !summary.data ? <SkeletonRows /> : s.length === 0 ? (
                  <Empty icon={<Receipt aria-hidden />} title="No billed requests yet">Send something from the Lab. Requests rejected at auth or limits never reach the ledger.</Empty>
                ) : (
                  <table className="tbl">
                    <thead><tr><th>{by}</th><th className="num">requests</th><th className="num">errors</th><th className="num">fell over</th><th className="num">tokens in</th><th className="num">tokens out</th><th className="num">cost</th><th style={{ width: "22%" }}>share of cost</th></tr></thead>
                    <tbody>
                      {s.map((r) => (
                        <tr key={r.group}>
                          <td className="mono" style={{ fontWeight: 600 }}>{r.group}</td>
                          <td className="num">{int(r.requests)}</td>
                          <td className="num">{int(r.errors)}</td>
                          <td className="num">{int(r.fallbacks)}</td>
                          <td className="num">{int(r.prompt_tokens)}</td>
                          <td className="num">{int(r.completion_tokens)}</td>
                          <td className="num">{usd(r.cost_usd)}</td>
                          <td><div className="meter" title={usd(r.cost_usd)}><i style={{ width: `${maxCost ? (r.cost_usd / maxCost) * 100 : 0}%` }} /></div></td>
                        </tr>
                      ))}
                      <tr style={{ fontWeight: 600 }}>
                        <td>total</td><td className="num">{int(tot.req)}</td><td className="num">{int(tot.err)}</td><td className="num">{int(tot.fb)}</td><td /><td /><td className="num">{usd(tot.cost)}</td><td />
                      </tr>
                    </tbody>
                  </table>
                )}
              </div>
            </section>
            <section className="section">
              <h2>Recent rows</h2>
              <p className="lead">Click a row to open its trace in the Lab, if the gateway still holds it in memory.</p>
              <div className="frame tblwrap" style={{ maxHeight: 520 }}>
                {rows.loading && !rows.data ? <SkeletonRows /> : !rows.data?.length ? <Empty title="Empty ledger" /> : (
                  <table className="tbl">
                    <thead><tr><th>when</th><th>key · team</th><th>model → provider</th><th>status</th><th className="num">att</th><th className="num">tok in/out</th><th className="num">latency</th><th className="num">cost</th></tr></thead>
                    <tbody>
                      {rows.data.map((r) => (
                        <tr key={r.id} className="clickable" onClick={() => go("lab", r.trace_id)} tabIndex={0} onKeyDown={(e) => e.key === "Enter" && go("lab", r.trace_id)}>
                          <td className="mono" title={r.ts}>{ago(r.ts)}</td>
                          <td className="mono">{r.key_name} · {r.team || "—"}</td>
                          <td className="mono">{r.model} → {r.provider}</td>
                          <td><StatusPill status={r.status} outcome={r.outcome} /></td>
                          <td className="num">{r.attempts}{r.fallbacks ? ` (${r.fallbacks}↓)` : ""}</td>
                          <td className="num">{r.prompt_tokens}/{r.completion_tokens}</td>
                          <td className="num">{ms(r.latency_ms)}</td>
                          <td className="num">{usd(r.cost_micro_usd / 1e6)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
              </div>
            </section>
          </>
        )}
      </div>
    </div>
  );
}
