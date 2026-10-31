import { ShieldCheck } from "lucide-react";
import { api, type SecurityInfo } from "../lib/api";
import { ago } from "../lib/format";
import { go } from "../lib/nav";
import { ApiProblem, Empty, SkeletonRows, usePoll } from "../components/state";

const ACTION_CLS: Record<string, string> = { suspend: "bad", block_ip: "bad", alert: "warn" };

/** The rules in force, in words, from the policy the gateway loaded. */
function rules(w: SecurityInfo["watch"], enforce: boolean): [string, string, string][] {
  const act = (a: string) => (enforce ? a : "alert only");
  return [
    ["R1 probe", `${w.probe_calls} refused tool calls in ${w.probe_window_s} s`, act("suspend the key")],
    ["R2 burn rate", `${w.burn_fraction * 100}% of the daily budget${w.burn_usd !== null ? ` (${`$${w.burn_usd}`} if it has none)` : ""} within ${w.burn_window_s} s`, act("suspend the key")],
    ["R3 new network", `a key's first request from a /24 unseen for ${w.new_network_days} days`, "alert"],
    ["R4 scanner", `${w.scanner_failures} failed key checks from one IP in ${w.scanner_window_s} s, or a bearer that isn't key-shaped`, act(`block the IP for ${w.block_ip_s} s`)],
    ["R5 egress", "the egress guard blocked a tool call that carried a secret", act("suspend the key")],
  ];
}

export default function Security() {
  const sec = usePoll(api.security, 4000);
  const keys = usePoll(api.keys, 10000);
  const held = (keys.data ?? []).filter((k) => k.status !== "active");
  const d = sec.data;

  return (
    <div className="page">
      <div className="page-in">
        <div className="page-head">
          <div>
            <h1>Security</h1>
            <p>
              A valid key doing invalid things is stopped at machine speed: small rules count events in the shared limit store, so every replica agrees.
              Automatic actions are reversible (suspend, never revoke; IP blocks expire). Secrets are looked for in tool arguments, tool results and prompts,
              and reported by type and position, never by value.{" "}
              <a href="#decisions/0011" onClick={(e) => { e.preventDefault(); go("decisions", "0011"); }}>ADR 0011</a>{" · "}
              <a href="#decisions/0012" onClick={(e) => { e.preventDefault(); go("decisions", "0012"); }}>ADR 0012</a>
            </p>
          </div>
        </div>
        {sec.error && !d ? <ApiProblem error={sec.error} /> : sec.loading && !d ? <SkeletonRows /> : (
          <>
            <section className="section" style={{ marginTop: 0 }}>
              <h2>State</h2>
              <p className="lead">
                <span className={`pill ${d!.enforce ? "ok" : "warn"}`}>{d!.enforce ? "enforcing" : "alert only"}</span>{" "}
                <span className={`pill ${d!.paid_off ? "bad" : "neutral"}`}>{d!.paid_off ? "lockdown: paid providers off" : "paid providers follow SLUICE_ALLOW_PAID"}</span>
                {d!.paid_off && <> Lift it with <code>python -m sluice.admin lockdown --lift</code>.</>}
              </p>
            </section>

            <section className="section">
              <h2>Keys held back</h2>
              <p className="lead">Suspended keys come back with <code>python -m sluice.admin unsuspend-key &lt;id&gt;</code>; revoked ones do not. The caller only ever sees a 401.</p>
              <div className="frame tblwrap">
                {!held.length ? <Empty icon={<ShieldCheck aria-hidden />} title="No suspended or revoked keys" /> : (
                  <table className="tbl">
                    <thead><tr><th>key</th><th>team</th><th>state</th><th>reason</th></tr></thead>
                    <tbody>
                      {held.map((k) => (
                        <tr key={k.id}>
                          <td className="mono" style={{ fontWeight: 600 }}>{k.name} <span style={{ color: "var(--ink-2)", fontWeight: 400 }}>#{k.id} · …{k.hint}</span></td>
                          <td className="mono">{k.team || "—"}</td>
                          <td><span className={`pill ${k.status === "revoked" ? "bad" : "warn"}`}>{k.status}</span></td>
                          <td className="mono">{k.suspended_reason ?? "—"}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
              </div>
            </section>

            <section className="section">
              <h2>Alerts</h2>
              <p className="lead">The newest {d!.alerts.length} kept in memory by this replica, newest first. Each is also a span event, a JSON log line and a metric.</p>
              <div className="frame tblwrap" style={{ maxHeight: 420 }}>
                {!d!.alerts.length ? <Empty icon={<ShieldCheck aria-hidden />} title="No alerts since this replica started" /> : (
                  <table className="tbl">
                    <thead><tr><th>when</th><th>rule</th><th>action</th><th>details</th></tr></thead>
                    <tbody>
                      {d!.alerts.map((a, i) => {
                        const { ts, rule, action, ...rest } = a;
                        return (
                          <tr key={i}>
                            <td className="mono" title={ts}>{ago(ts)}</td>
                            <td className="mono">{rule}</td>
                            <td><span className={`pill ${ACTION_CLS[action] ?? "neutral"}`}>{action}</span></td>
                            <td className="mono">{Object.entries(rest).map(([k, v]) => `${k}=${v}`).join(" · ")}</td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                )}
              </div>
            </section>

            <section className="section">
              <h2>Rules in force</h2>
              <div className="frame tblwrap">
                <table className="tbl">
                  <thead><tr><th>rule</th><th>fires on</th><th>then</th></tr></thead>
                  <tbody>
                    {rules(d!.watch, d!.enforce).map(([r, when, then]) => (
                      <tr key={r}><td className="mono">{r}</td><td>{when}</td><td>{then}</td></tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <p className="lead" style={{ marginTop: 12 }}>
                Secret guard: prompts <code>{d!.guard.chat}</code> · tool arguments{" "}
                {Object.entries(d!.guard.tool_args).map(([g, m]) => <code key={g}>{g}: {m} </code>)}· tool results{" "}
                {Object.entries(d!.guard.tool_results).map(([g, m]) => <code key={g}>{g}: {m} </code>)}
              </p>
            </section>
          </>
        )}
      </div>
    </div>
  );
}
