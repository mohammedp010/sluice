import { KeyRound } from "lucide-react";
import { api } from "../lib/api";
import { usd } from "../lib/format";
import { ApiProblem, Empty, SkeletonRows, usePoll } from "../components/state";

function Budget({ spent, cap }: { spent: number; cap: number | null }) {
  if (cap === null) return <span className="num" style={{ color: "var(--ink-2)" }}>{usd(spent)} · no cap</span>;
  const f = Math.min(1, spent / cap);
  return (
    <div style={{ display: "grid", gap: 4, minWidth: 120 }}>
      <span className="num">{usd(spent)} of {usd(cap)}{f >= 1 ? " · exhausted" : ""}</span>
      <div className={`meter ${f >= 1 ? "bad" : f > 0.8 ? "warn" : ""}`} role="meter" aria-valuemin={0} aria-valuemax={cap} aria-valuenow={spent}>
        <i style={{ width: `${f * 100}%` }} />
      </div>
    </div>
  );
}

export default function Keys() {
  const keys = usePoll(api.keys, 4000);
  return (
    <div className="page">
      <div className="page-in">
        <div className="page-head">
          <div>
            <h1>Virtual keys</h1>
            <p>What clients present to Sluice. They never map to provider credentials. Only a SHA-256 of each key is stored; this view shows the last four characters, as the admin CLI does. Spend is live from the limit store.</p>
          </div>
        </div>
        {keys.error && !keys.data ? <ApiProblem error={keys.error} /> : (
          <div className="frame tblwrap">
            {keys.loading && !keys.data ? <SkeletonRows /> : !keys.data?.length ? (
              <Empty icon={<KeyRound aria-hidden />} title="No keys yet">
                Create one from the repo root: <code>python -m sluice.admin create-key --name lab --team search --rpm 120</code>
              </Empty>
            ) : (
              <table className="tbl">
                <thead><tr><th>key</th><th>team · app</th><th>models</th><th className="num">rpm</th><th className="num">tpm</th><th>today</th><th>this month</th><th>MCP tools</th></tr></thead>
                <tbody>
                  {keys.data.map((k) => (
                    <tr key={k.id} style={k.revoked ? { opacity: 0.55 } : undefined}>
                      <td>
                        <div className="mono" style={{ fontWeight: 600 }}>{k.name}</div>
                        <div className="mono" style={{ color: "var(--ink-2)" }}>#{k.id} · …{k.hint}{k.revoked && <> · <span className="pill bad">revoked</span></>}</div>
                      </td>
                      <td className="mono">{k.team || "—"} · {k.app || "—"}</td>
                      <td className="mono">{k.allowed_models.join(", ")}</td>
                      <td className="num">{k.rpm.toLocaleString("en-US")}</td>
                      <td className="num">{k.tpm.toLocaleString("en-US")}</td>
                      <td><Budget spent={k.spend_today_usd} cap={k.daily_budget_usd} /></td>
                      <td><Budget spent={k.spend_month_usd} cap={k.monthly_budget_usd} /></td>
                      <td className="mono">{k.allowed_tools.length ? k.allowed_tools.join(", ") : <span style={{ color: "var(--ink-3)" }}>none (deny by default)</span>}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
