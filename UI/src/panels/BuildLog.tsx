import commits, { type Commit } from "virtual:git-log";
import { adrsForPhase, notes } from "../lib/docs";
import { go } from "../lib/nav";
import Markdown from "../components/Markdown";
import { Empty } from "../components/state";

/** Phases a commit belongs to, read from its subject ("Phase 6: …", "Sluice phases 1-3: …"). */
function phasesOf(c: Commit): number[] {
  const range = /phases (\d)\s*-\s*(\d)/i.exec(c.subject);
  if (range) {
    const [a, b] = [Number(range[1]), Number(range[2])];
    return Array.from({ length: b - a + 1 }, (_, i) => a + i);
  }
  const one = /phase (\d)/i.exec(c.subject);
  return one ? [Number(one[1])] : [];
}

const clean = (body: string) => body.replace(/\n*Co-Authored-By:.*$/gim, "").trim();

export default function BuildLog({ selected }: { selected: string }) {
  if (!commits.length) {
    return <Empty title="No git history found">The build log reads <code>git log</code> when the dev server starts. Run it from a git checkout.</Empty>;
  }
  const newestFirst = [...commits].reverse();
  const cur = newestFirst.find((c) => c.hash === selected) ?? newestFirst[0];
  const phases = phasesOf(cur);
  const total = commits.reduce((n, c) => n + c.added, 0);

  return (
    <div className="split" style={{ gridTemplateColumns: "minmax(320px, 520px) minmax(0, 1fr)" }}>
      <section className="pane" aria-label="Revisions">
        <div className="pane-h">
          <h2>Revisions</h2><span className="grow" />
          <span className="num">{commits.length} commits · +{total.toLocaleString("en-US")} lines</span>
        </div>
        <div className="pane-b">
          <ol className="revs" reversed>
            {newestFirst.map((c) => (
              <li key={c.hash} className="rev" aria-selected={c === cur} tabIndex={0}
                onClick={() => go("build", c.hash)} onKeyDown={(e) => e.key === "Enter" && go("build", c.hash)}>
                <span className="d">{c.date}</span>
                <span className="h">{c.hash}</span>
                <span className="s">{c.subject}</span>
                <span className="st"><span className="a">+{c.added}</span> <span className="r">−{c.removed}</span></span>
              </li>
            ))}
          </ol>
        </div>
      </section>
      <section className="pane" aria-label={cur.subject}>
        <div className="pane-h"><h2 className="mono">{cur.hash}</h2><span>{cur.date} · {cur.files} files</span></div>
        <div className="pane-b">
          <div className="ev">
            <div className="ev-head">
              <h1>{cur.subject}</h1>
              <div className="ev-src">
                {phases.map((p) => <span key={p} className="pill accent">Phase {p}</span>)}
                {!phases.length && <span className="pill warn">fix between phases</span>}
                {phases.flatMap(adrsForPhase).map((a) => (
                  <a key={a.n} href={`#decisions/${a.n}`} onClick={(e) => { e.preventDefault(); go("decisions", a.n); }}>ADR {a.n}: {a.title}</a>
                ))}
              </div>
            </div>
            {clean(cur.body) && <pre className="cmd" style={{ background: "var(--chrome)", padding: "12px 14px", lineHeight: 1.6 }}>{clean(cur.body)}</pre>}
            {phases.map((p) => notes[`phase-${p}.md`] && (
              <div key={p} className="figure">
                <h3>Phase {p}: questions to answer in your own words</h3>
                <Markdown>{notes[`phase-${p}.md`].replace(/^# .*\n/, "")}</Markdown>
              </div>
            ))}
          </div>
        </div>
      </section>
    </div>
  );
}
