import { useEffect, useState } from "react";
import {
  BookOpen, Dam, FlaskConical, GitCommitVertical, KeyRound, Network, Receipt, ScrollText, Wrench,
} from "lucide-react";
import { api, type BreakerState } from "./lib/api";
import { go, type PanelId } from "./lib/nav";
import { BreakerMark, breakerHelp } from "./components/marks";
import { usePoll } from "./components/state";
import Lab from "./panels/Lab";
import Topology from "./panels/Topology";
import Ledger from "./panels/Ledger";
import Evidence from "./panels/Evidence";
import Decisions from "./panels/Decisions";
import BuildLog from "./panels/BuildLog";
import Mcp from "./panels/Mcp";
import Keys from "./panels/Keys";

const PANELS: { id: PanelId; label: string; icon: typeof FlaskConical }[] = [
  { id: "lab", label: "Lab", icon: FlaskConical },
  { id: "topology", label: "Topology", icon: Network },
  { id: "ledger", label: "Ledger", icon: Receipt },
  { id: "evidence", label: "Evidence", icon: ScrollText },
  { id: "decisions", label: "Decisions", icon: BookOpen },
  { id: "build", label: "Build log", icon: GitCommitVertical },
  { id: "mcp", label: "MCP", icon: Wrench },
  { id: "keys", label: "Keys", icon: KeyRound },
];

/** `#panel/arg`: the panel and an optional selection inside it (a trace id, an ADR, ...). */
function readHash(): { panel: PanelId; arg: string } {
  const [p, ...rest] = location.hash.replace(/^#\/?/, "").split("/");
  const panel = (PANELS.some((x) => x.id === p) ? p : "lab") as PanelId;
  return { panel, arg: decodeURIComponent(rest.join("/")) };
}

export default function App() {
  const [{ panel, arg }, setRoute] = useState(readHash);
  useEffect(() => {
    const on = () => setRoute(readHash());
    addEventListener("hashchange", on);
    return () => removeEventListener("hashchange", on);
  }, []);

  const overview = usePoll(api.overview, 15000);
  const health = usePoll(api.health, 2000);
  const online = !health.error || health.error.kind !== "offline";

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <Dam width={18} height={18} strokeWidth={2.1} aria-hidden />
          <span className="bt">Sluice</span> <small>workbench</small>
        </div>
        <nav className="tabs" role="tablist" aria-label="Panels">
          {PANELS.map(({ id, label, icon: I }) => (
            <button key={id} role="tab" className="tab" aria-selected={panel === id} onClick={() => go(id)} aria-label={label} title={label}>
              <I aria-hidden />
              <span className="lbl">{label}</span>
            </button>
          ))}
        </nav>
        {health.data && (() => {
          const all = Object.entries(health.data) as [string, BreakerState][];
          const tripped = all.filter(([, st]) => st !== "closed");
          return (
            <div className="lamps" aria-label="Circuit breakers">
              {tripped.map(([name, st]) => (
                <button key={name} className="lamp" title={`${name}: ${breakerHelp(st)}`} onClick={() => go("topology", name)}>
                  <BreakerMark state={st} withWord={false} />
                  <span className="lt">{name}</span>
                </button>
              ))}
              <button className="lamp" onClick={() => go("topology")} title={all.filter(([, st]) => st === "closed").map(([n]) => n).join(", ")}>
                <BreakerMark state="closed" withWord={false} />
                <span className="lt">{tripped.length ? `${all.length - tripped.length} more closed` : `${all.length} breakers closed`}</span>
                <span className="lc">{all.length - tripped.length}</span>
              </button>
            </div>
          );
        })()}
        <div className="conn" role="status">
          <span className={`mark ${online ? "v-pass" : "v-fail"}`}>
            <svg width="8" height="8" aria-hidden><circle cx="4" cy="4" r="4" fill="currentColor" /></svg>
          </span>
          <span className="lbl">{online ? ":8000" : "offline"}</span>
        </div>
      </header>
      <main className="main">
        {panel === "lab" && <Lab overview={overview} health={health.data} traceArg={arg} />}
        {panel === "topology" && <Topology overview={overview} focus={arg} />}
        {panel === "ledger" && <Ledger />}
        {panel === "evidence" && <Evidence selected={arg} />}
        {panel === "decisions" && <Decisions selected={arg} />}
        {panel === "build" && <BuildLog selected={arg} />}
        {panel === "mcp" && <Mcp />}
        {panel === "keys" && <Keys />}
      </main>
    </div>
  );
}
