import { adrList } from "../lib/docs";
import { go } from "../lib/nav";
import Markdown from "../components/Markdown";

// Where each decision lives in the system, so a reader can jump from a stage to its "why".
export const ADR_AREA: Record<string, string> = {
  "0001": "streaming", "0002": "executor", "0003": "limits", "0004": "telemetry", "0005": "cache",
  "0006": "quality", "0007": "performance", "0008": "streaming", "0009": "mcp",
};

export default function Decisions({ selected }: { selected: string }) {
  const cur = adrList.find((a) => a.n === selected) ?? adrList[0];
  return (
    <div className="split">
      <section className="pane" aria-label="Decision records">
        <div className="pane-h"><h2>Decision records</h2><span className="grow" /><span>{adrList.length}</span></div>
        <div className="pane-b">
          <ul className="claims">
            {adrList.map((a) => (
              <li key={a.n} className="claim" aria-selected={a === cur} tabIndex={0}
                onClick={() => go("decisions", a.n)} onKeyDown={(e) => e.key === "Enter" && go("decisions", a.n)}>
                <span style={{ display: "flex", gap: 8, alignItems: "center" }}>
                  <span className="num" style={{ color: "var(--ink-2)", fontSize: "var(--fs-sm)" }}>{a.n}</span>
                  <span className="pill neutral">{ADR_AREA[a.n] ?? "general"}</span>
                </span>
                <span className="ttl">{a.title}</span>
                <span className="res">{a.status.length > 90 ? a.status.slice(0, 88) + "…" : a.status}</span>
              </li>
            ))}
          </ul>
        </div>
      </section>
      <section className="pane" aria-label={cur?.title}>
        <div className="pane-h"><h2 className="mono">docs/decisions/{cur?.file}</h2></div>
        <div className="pane-b"><div className="ev">{cur && <Markdown>{cur.body}</Markdown>}</div></div>
      </section>
    </div>
  );
}
