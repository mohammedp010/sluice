import { Check, Minus, Wrench } from "lucide-react";
import { api } from "../lib/api";
import { ago, ms } from "../lib/format";
import { go } from "../lib/nav";
import { ApiProblem, Empty, SkeletonRows, usePoll } from "../components/state";

/** fnmatch-style glob, as VirtualKey.allows_tool uses (case-sensitive). */
const globMatch = (pattern: string, name: string) =>
  new RegExp(`^${pattern.replace(/[.+^${}()|\\]/g, "\\$&").replace(/\*/g, ".*").replace(/\?/g, ".")}$`).test(name);

const OUTCOME_CLS: Record<string, string> = { ok: "ok", denied: "bad", unknown: "warn", error: "bad", tool_error: "warn", quarantined: "bad", pending: "warn" };
const PIN_CLS: Record<string, string> = { approved: "ok", pending: "warn", quarantined: "bad" };

export default function Mcp() {
  const mcp = usePoll(api.mcp, 4000);
  const keys = usePoll(api.keys, 10000);
  const err = mcp.error ?? keys.error;
  const tools = [...new Set((mcp.data?.audit ?? []).map((a) => a.tool))].sort();

  return (
    <div className="page">
      <div className="page-in">
        <div className="page-head">
          <div>
            <h1>MCP proxy</h1>
            <p>
              Tool servers behind <code>POST /mcp</code>, merged as <code>server__tool</code>. Keys see only the tools their globs grant (deny by default),
              and a denied tool looks exactly like one that doesn't exist. Every <code>tools/call</code> is audited, denials included, with a hash of
              the arguments but never the arguments themselves.{" "}
              <a href="#decisions/0009" onClick={(e) => { e.preventDefault(); go("decisions", "0009"); }}>ADR 0009</a>
            </p>
          </div>
        </div>
        {err && !mcp.data ? <ApiProblem error={err} /> : mcp.loading && !mcp.data ? <SkeletonRows /> : (
          <>
            <section className="section" style={{ marginTop: 0 }}>
              <h2>Servers</h2>
              <p className="lead">
                {Object.keys(mcp.data!.servers).length
                  ? Object.entries(mcp.data!.servers).map(([n, k]) => `${n} (${k})`).join(" · ")
                  : <>None configured in this policy. Add one under <code>mcp.servers</code>, for example <code>{`files: {command: ["npx", "-y", "@modelcontextprotocol/server-filesystem", "/tmp"]}`}</code>.</>}
              </p>
            </section>

            <section className="section">
              <h2>Grants</h2>
              <p className="lead">Each key's globs, matched against every tool name seen in the audit log.</p>
              <div className="frame tblwrap">
                {!keys.data?.length ? <Empty title="No keys" /> : (
                  <table className="tbl matrix">
                    <thead>
                      <tr><th>key</th><th>globs</th>{tools.map((t) => <th key={t} className="mono" style={{ textAlign: "center" }}>{t}</th>)}</tr>
                    </thead>
                    <tbody>
                      {keys.data.map((k) => (
                        <tr key={k.id}>
                          <td className="mono" style={{ fontWeight: 600 }}>{k.name}</td>
                          <td className="mono">{k.allowed_tools.join(", ") || <span style={{ color: "var(--ink-3)" }}>none</span>}</td>
                          {tools.map((t) => {
                            const ok = k.allowed_tools.some((g) => globMatch(g, t));
                            return (
                              <td key={t} className="cell" title={ok ? "granted" : "denied"}>
                                {ok ? <Check className="v-pass" aria-label="granted" /> : <Minus className="v-skip" aria-label="denied" />}
                              </td>
                            );
                          })}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
                {!tools.length && keys.data?.length ? <p style={{ padding: 12, fontSize: "var(--fs-sm)", color: "var(--ink-2)" }}>No tool calls audited yet, so there are no tool columns.</p> : null}
              </div>
            </section>

            <section className="section">
              <h2>Pins</h2>
              <p className="lead">
                Each tool definition is hashed when first seen. A changed one is quarantined: hidden and refused like an unknown tool until
                {" "}<code>python -m sluice.admin tools diff</code> and <code>tools approve</code>. New tools that trip the lint wait as pending.
              </p>
              <div className="frame tblwrap">
                {!mcp.data!.pins.length ? (
                  <Empty icon={<Wrench aria-hidden />} title="No tools pinned yet">Tools are pinned the first time the gateway lists them.</Empty>
                ) : (
                  <table className="tbl">
                    <thead><tr><th>tool</th><th>status</th><th>trusted · seen</th><th>changed</th><th>lint</th></tr></thead>
                    <tbody>
                      {mcp.data!.pins.map((p) => {
                        const flags: string[] = JSON.parse(p.flags);
                        return (
                          <tr key={p.tool}>
                            <td className="mono">{p.tool}</td>
                            <td><span className={`pill ${PIN_CLS[p.status] ?? "neutral"}`}>{p.status}</span></td>
                            <td className="mono">{p.pinned_sha256.slice(0, 8)}{p.seen_sha256 !== p.pinned_sha256 && <> → {p.seen_sha256.slice(0, 8)}</>}</td>
                            <td className="mono">{p.changed_at ? `${p.changed_at} UTC` : "—"}</td>
                            <td className="mono">{flags.length ? flags.join("; ") : "—"}</td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                )}
              </div>
            </section>

            <section className="section">
              <h2>Audit log</h2>
              <p className="lead">The same rows as <code>python -m sluice.report --mcp</code>, newest first.</p>
              <div className="frame tblwrap" style={{ maxHeight: 480 }}>
                {!mcp.data!.audit.length ? (
                  <Empty icon={<Wrench aria-hidden />} title="No tool calls yet">Point an MCP client at <code>http://localhost:8000/mcp</code> with a key that has <code>--tools</code> grants.</Empty>
                ) : (
                  <table className="tbl">
                    <thead><tr><th>when</th><th>key · team</th><th>tool</th><th>outcome</th><th className="num">args</th><th className="num">latency</th><th>error</th></tr></thead>
                    <tbody>
                      {mcp.data!.audit.map((a) => (
                        <tr key={a.id}>
                          <td className="mono" title={a.ts}>{ago(a.ts)}</td>
                          <td className="mono">{a.key_name} · {a.team || "—"}</td>
                          <td className="mono">{a.tool}</td>
                          <td><span className={`pill ${OUTCOME_CLS[a.outcome] ?? "neutral"}`}>{a.outcome}</span></td>
                          <td className="num" title={a.args_sha256 ?? ""}>{a.args_bytes ?? 0} B · {a.args_sha256?.slice(0, 8) ?? "—"}</td>
                          <td className="num">{a.latency_ms != null ? ms(a.latency_ms) : "—"}</td>
                          <td className="mono">{a.error ?? ""}</td>
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
