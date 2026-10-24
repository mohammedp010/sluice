import { useState, type ReactNode } from "react";
import { ArrowRight, BookOpen, Bug, Check, FileText, FlaskConical, Minus, Scale, TriangleAlert } from "lucide-react";
import { resultJson, results } from "../lib/docs";
import { go } from "../lib/nav";
import Markdown from "../components/Markdown";
import { DataTable, DotCI, HBars, Legend, LineChart, SERIES } from "../components/charts";

// Every claim in the README's Results table, each drawn from its own result file in
// bench/results/. Negative results and losses sit in the same list, at the same size.

type Verdict = "holds" | "negative" | "bug" | "loses" | "caveat";

const VERDICT: Record<Verdict, { label: string; cls: string; icon: typeof Check }> = {
  holds: { label: "holds", cls: "ok", icon: Check },
  negative: { label: "negative result", cls: "warn", icon: Minus },
  bug: { label: "bug found + fixed", cls: "hit", icon: Bug },
  loses: { label: "Sluice loses", cls: "bad", icon: Scale },
  caveat: { label: "holds, with a caveat", cls: "warn", icon: TriangleAlert },
};

interface Claim {
  id: string;
  title: string;
  claim: string;
  verdict: Verdict;
  scripts: string[];
  files: string[];
  adrs: string[];
  lab?: string; // a lab route that shows it live
  figure: () => ReactNode;
}

// ---------------------------------------------------------------- data access

type Chaos = Record<string, { requests: number; success_rate: number; served_by_primary: number; served_by_backup: number; p50_ms: number; p95_ms: number }>;
const chaos = resultJson["chaos.json"] as Chaos & {
  _recovery: { breaker_cooldown_s: number; seconds_until_primary_serves_again: number };
  _stream_cut_mid_stream: { http_status: number; got_done_marker: boolean; got_error_event: boolean };
};
const scenarios = Object.entries(chaos).filter(([k]) => !k.startsWith("_")) as [string, Chaos[string]][];

const ratelimit = resultJson["ratelimit.json"] as Record<string, { rpm_limit: number; sent: number; "200": number; "429": number; exact: boolean }>;

interface CacheCfg {
  label: string; guard: number | null; threshold: number;
  curve_dev: { t: number; precision: number | null; recall: number }[];
  test: { tp: number; fp: number; fn: number; precision: number | null; recall: number };
}
const cache = resultJson["cache-eval.json"] as CacheCfg[];

interface Policy { accuracy: number; acc_lo: number; acc_hi: number; rel_cost: number; cost_lo: number; cost_hi: number; escalated: number; n: number }
const routing = {
  mistral: (resultJson["routing-eval-mistral.json"] as { policies: Record<string, Policy> }).policies,
  "qwen2.5": (resultJson["routing-eval-qwen2.5.json"] as { policies: Record<string, Policy> }).policies,
};

interface Run { target: string; rps_target: number; rep: number; p50_ms: number; p99_ms: number; success_rate: number; gen_lag_p99_ms: number }
const compare = resultJson["compare-after.json"] as { sweep: Run[]; failover: Run[] };

/** compare_report.py's rule: drop reps where the load generator was >20 ms behind, then median. */
function sweepMedian(target: string, key: "p50_ms" | "p99_ms"): [number, number][] {
  const rates = [...new Set(compare.sweep.map((r) => r.rps_target))].sort((a, b) => a - b);
  return rates.map((rps) => {
    const runs = compare.sweep.filter((r) => r.target === target && r.rps_target === rps);
    const clean = runs.filter((r) => r.gen_lag_p99_ms <= 20);
    const vs = (clean.length ? clean : runs).map((r) => r[key]).sort((a, b) => a - b);
    const mid = Math.floor(vs.length / 2);
    return [rps, vs.length % 2 ? vs[mid] : (vs[mid - 1] + vs[mid]) / 2];
  });
}

/** Rows of the first markdown table after `after` in a result file. */
function mdTable(file: string, after = ""): string[][] {
  const md = results[file] ?? "";
  const from = after ? md.indexOf(after) : 0;
  const lines = md.slice(Math.max(0, from)).split("\n");
  const start = lines.findIndex((l) => l.startsWith("|"));
  const out: string[][] = [];
  for (const l of lines.slice(start + 2)) {
    if (!l.startsWith("|")) break;
    out.push(l.split("|").slice(1, -1).map((c) => c.replace(/\*\*/g, "").trim()));
  }
  return out;
}

function mdSection(file: string, heading: string): string {
  const md = results[file] ?? "";
  const i = md.indexOf(`## ${heading}`);
  if (i < 0) return "";
  const rest = md.slice(i);
  const j = rest.indexOf("\n## ", 4);
  return j < 0 ? rest : rest.slice(0, j);
}

const ms = (v: number) => `${Math.round(v)} ms`;
const pct = (v: number) => `${Math.round(v * 100)}%`;

// ---------------------------------------------------------------- the claims

const CLAIMS: Claim[] = [
  {
    id: "failover", title: "Failover under injected faults", verdict: "holds",
    claim: `100% success in all ${scenarios.length} fault scenarios; a recovered primary serves again after ${chaos._recovery.seconds_until_primary_serves_again} s (breaker cooldown ${chaos._recovery.breaker_cooldown_s} s).`,
    scripts: ["bench/chaos.py"], files: ["chaos.md", "chaos.json"], adrs: ["0002"], lab: "lab-failover",
    figure: () => (
      <>
        <Fig title="Who served the 200 requests in each scenario" cap="Stacked: requests answered by the primary versus the backup. Every scenario ends at 200 because every request succeeded.">
          <Legend items={[{ label: "primary", color: SERIES[0] }, { label: "backup", color: SERIES[1] }]} />
          <HBars stacked series={SERIES} rows={scenarios.map(([k, v]) => ({ label: k, values: [v.served_by_primary, v.served_by_backup], note: pct(v.success_rate) + " ok" }))} max={200} />
        </Fig>
        <Fig title="What failover costs: p95 latency per scenario" cap="p50 barely moves (about 21 ms throughout). Retries and timeouts show up in the tail.">
          <HBars series={[SERIES[0]]} rows={scenarios.map(([k, v]) => ({ label: k, values: [v.p95_ms] }))} format={ms} />
          <DataTable head={["scenario", "success", "primary", "backup", "p50 ms", "p95 ms"]}
            rows={scenarios.map(([k, v]) => [k, pct(v.success_rate), v.served_by_primary, v.served_by_backup, v.p50_ms, v.p95_ms])} />
        </Fig>
      </>
    ),
  },
  {
    id: "stream-cut", title: "Mid-stream failure", verdict: "holds",
    claim: "A stream that dies after its first byte ends with a terminal SSE error event and no [DONE]. It is never silently restarted.",
    scripts: ["bench/chaos.py"], files: ["chaos.md"], adrs: ["0001"], lab: "lab-stream-cut-off",
    figure: () => (
      <Fig title="The cut stream, as the client saw it" cap="The status line was already sent (200), so the failure can only be reported inside the stream.">
        <div className="kv">
          <div>http status</div><div>{chaos._stream_cut_mid_stream.http_status}</div>
          <div>error event</div><div>{chaos._stream_cut_mid_stream.got_error_event ? "yes" : "no"}</div>
          <div>[DONE] marker</div><div>{chaos._stream_cut_mid_stream.got_done_marker ? "yes" : "no"}</div>
        </div>
      </Fig>
    ),
  },
  {
    id: "limits", title: "Limits hold across 2 replicas", verdict: "holds",
    claim: `Exact: ${Object.values(ratelimit).map((v) => `${v["200"]}/${v.sent}`).join(", ")} allowed for keys limited to ${Object.values(ratelimit).map((v) => v.rpm_limit).join(", ")} RPM, with load split over two gateways that share one Redis.`,
    scripts: ["bench/ratelimit.py"], files: ["ratelimit.json"], adrs: ["0003"],
    figure: () => (
      <Fig title="Requests allowed per key (of 100 sent), against its RPM limit" cap="The black tick is the key's limit. The bar ends exactly on it every time: no request leaked through either replica.">
        <HBars series={[SERIES[0]]} max={100} rows={Object.entries(ratelimit).map(([k, v]) => ({ label: `${k} (${v.rpm_limit} rpm)`, values: [v["200"]], note: `${v["429"]} got 429` }))}
          marker={(r) => ratelimit[r.label.split(" ")[0]].rpm_limit} />
        <DataTable head={["key", "limit", "sent", "200", "429", "exact"]} rows={Object.entries(ratelimit).map(([k, v]) => [k, v.rpm_limit, v.sent, v["200"], v["429"], v.exact ? "yes" : "no"])} />
      </Fig>
    ),
  },
  {
    id: "trace", title: "A failed-over request explained by its trace", verdict: "holds",
    claim: "Span tree plus cost row, joined by trace id: two 503s on the primary, one retry, a fallback, and the backup's prices billed to the key's team.",
    scripts: ["bench/trace_demo.py"], files: ["trace-failover.md"], adrs: ["0004"], lab: "lab-failover",
    figure: () => <Fig title="The recorded trace" cap="The Lab draws the same thing live for any request you send."><Markdown>{(results["trace-failover.md"] ?? "").replace(/^# .*\n/, "")}</Markdown></Fig>,
  },
  {
    id: "cache", title: "Semantic cache", verdict: "caveat",
    claim: "86% hit rate on true repeats, but 3 of 33 hits were wrong (9%) on held-out pairs. Without the lexical guard, no threshold was usable. It is off on the Ollama route.",
    scripts: ["bench/cache_eval.py"], files: ["cache-eval.md", "cache-eval.json"], adrs: ["0005"],
    figure: () => <CacheFigure />,
  },
  {
    id: "routing", title: "Cheap-first routing", verdict: "negative",
    claim: "No gain. The 1B model alone scored 82%, against 80% for always-7B, and the free checks fired on 1 prompt in 60.",
    scripts: ["bench/routing_collect.py", "bench/routing_eval.py"], files: ["routing-eval-mistral.md", "routing-eval-qwen2.5.md"], adrs: ["0006"], lab: "lab-escalate",
    figure: () => <RoutingFigure />,
  },
  {
    id: "overhead", title: "Overhead vs LiteLLM, same mock upstream", verdict: "holds",
    claim: "p50: Sluice adds 0 to 5 ms, LiteLLM 2 to 8 ms. p99 at 150–250 rps: Sluice is within noise of direct, LiteLLM adds 42 to 147 ms.",
    scripts: ["bench/compare.py", "bench/compare_report.py"], files: ["compare.md", "compare-after.json"], adrs: ["0007"],
    figure: () => {
      const targets = [["sluice", "Sluice"], ["litellm", "LiteLLM"], ["direct", "direct (no gateway)"]] as const;
      const lines = (k: "p50_ms" | "p99_ms") => targets.map(([t, label], i) => ({ label, color: SERIES[i], points: sweepMedian(t, k) }));
      return (
        <>
          <Legend items={targets.map(([, l], i) => ({ label: l, color: SERIES[i] }))} />
          <Fig title="p50 latency vs request rate (median of clean reps)" cap="The 50 ms mock upstream is the floor. Both gateways stay close to direct at the median.">
            <LineChart lines={lines("p50_ms")} xLabel="rps" yFormat={ms} yMax={80} height={220} />
          </Fig>
          <Fig title="p99 latency vs request rate" cap="This is where they differ: LiteLLM's tail grows from 150 rps, while Sluice's stays near direct. One laptop ran everything; see the caveats in compare.md.">
            <LineChart lines={lines("p99_ms")} xLabel="rps" yFormat={ms} height={240} />
            <DataTable head={["rps", ...targets.flatMap(([, l]) => [`${l} p50`, `${l} p99`])]}
              rows={sweepMedian("sluice", "p50_ms").map(([rps], i) => [rps, ...targets.flatMap(([t]) => [sweepMedian(t, "p50_ms")[i][1].toFixed(1), sweepMedian(t, "p99_ms")[i][1].toFixed(1)])])} />
          </Fig>
        </>
      );
    },
  },
  {
    id: "failover-vs", title: "Failover vs LiteLLM (30% faulty primary)", verdict: "holds",
    claim: "Both gateways reached 100% success. p99 was 228 ms for Sluice and 2.8 s for LiteLLM (one run each).",
    scripts: ["bench/compare.py"], files: ["compare.md"], adrs: ["0002"], lab: "lab-flaky",
    figure: () => (
      <Fig title="p99 latency with a 30%-failing primary, 50 rps" cap="Direct calls see the raw failure rate. Each gateway hides it by retrying, and pays for that in the tail. One 20 s run each.">
        <HBars series={[SERIES[0]]} rows={compare.failover.map((r) => ({ label: r.target, values: [r.p99_ms], note: `${pct(r.success_rate)} success` }))} format={ms} />
        <DataTable head={["target", "success", "p50 ms", "p99 ms"]} rows={compare.failover.map((r) => [r.target, pct(r.success_rate), r.p50_ms.toFixed(0), r.p99_ms.toFixed(0)])} />
      </Fig>
    ),
  },
  {
    id: "features", title: "Where Sluice loses to LiteLLM", verdict: "loses",
    claim: "Providers, an admin UI, key management, per team and per user budgets, integrations, maturity.",
    scripts: ["bench/compare_report.py"], files: ["compare.md"], adrs: [],
    figure: () => <Fig title="Feature comparison, from compare.md" cap="Daggers mark LiteLLM features taken from its docs, not tested here."><Markdown>{mdSection("compare.md", "Feature comparison").replace(/^## .*\n/, "")}</Markdown></Fig>,
  },
  {
    id: "pool-leak", title: "A bug found by benchmarking", verdict: "bug",
    claim: "Cancelled httpx calls leaked pool slots (22 → 100, then dead). Fixed: success after an overload went from 0% to 100%.",
    scripts: ["bench/httpx_pool_leak.py"], files: ["httpx-pool-leak.md"], adrs: ["0007"],
    figure: () => {
      const rows = mdTable("httpx-pool-leak.md").filter((r) => /^\d+$/.test(r[1]));
      const rounds = [...new Set(rows.map((r) => Number(r[1])))];
      const get = (mode: string) => rounds.map((n) => [n, Number(rows.find((r) => r[0] === mode && Number(r[1]) === n)?.[2] ?? 0)] as [number, number]);
      return (
        <Fig title="Pool entries stuck in CONNECTING, per round of 1,500 requests" cap="Cancelling mid-connect (asyncio.timeout) strands entries until all 100 are dead. httpx's own timeouts clean up.">
          <Legend items={[{ label: "cancel (before)", color: SERIES[1] }, { label: "native timeouts (after)", color: SERIES[0] }]} />
          <LineChart lines={[{ label: "cancel (before)", color: SERIES[1], points: get("cancel") }, { label: "native timeouts (after)", color: SERIES[0], points: get("native") }]}
            xLabel="round" yFormat={(v) => String(Math.round(v))} yMax={100} height={220} />
        </Fig>
      );
    },
  },
  {
    id: "profile", title: "Top profiling hotspot", verdict: "bug",
    claim: "A per-request SQLite commit on the event loop took ~19% of CPU. Ledger writes are now batched off the loop.",
    scripts: ["bench/profile_sluice.py"], files: ["profile-before-flat.txt", "profile-after-flat.txt"], adrs: ["0007"],
    figure: () => (
      <>
        <Fig title="Before: flat profile, top lines" cap="Connection.__exit__ and Connection.execute are the sqlite3 commit and insert, run on the event loop for every request.">
          <pre className="cmd" style={{ maxHeight: 260, overflow: "auto" }}>{(results["profile-before-flat.txt"] ?? "").split("\n").slice(7, 22).join("\n")}</pre>
        </Fig>
        <Fig title="After: flat profile, top lines" cap="The sqlite calls are gone from the top of the profile.">
          <pre className="cmd" style={{ maxHeight: 260, overflow: "auto" }}>{(results["profile-after-flat.txt"] ?? "").split("\n").slice(7, 22).join("\n")}</pre>
        </Fig>
      </>
    ),
  },
  {
    id: "resume", title: "Stream resume after a mid-answer failure", verdict: "holds",
    claim: "Prefill was clean 30/30 on the same model and 29/30 on another. A silent restart was clean 0/30.",
    scripts: ["bench/stream_resume_eval.py"], files: ["stream-resume.md"], adrs: ["0008", "0001"], lab: "lab-stream-cut",
    figure: () => {
      const rows = mdTable("stream-resume.md");
      const strategies = [...new Set(rows.map((r) => r[0]))];
      const models = [...new Set(rows.map((r) => r[1]))];
      const clean = (s: string, m: string) => Number(rows.find((r) => r[0] === s && r[1] === m)?.[2].split("/")[0] ?? 0);
      return (
        <Fig title="Clean continuations out of 30, per strategy and backup model" cap="Clean means the answer is numbered exactly 1..N, with no repeats, gaps or restarts. 'error' is ADR 0001's default: honest, but never complete.">
          <Legend items={models.map((m, i) => ({ label: `backup: ${m}`, color: SERIES[i] }))} />
          <HBars series={SERIES} max={30} rows={strategies.map((s) => ({ label: s, values: models.map((m) => clean(s, m)) }))} format={(v) => `${v}/30`} />
          <DataTable head={["strategy", "backup", "clean", "repeated", "restarted", "gaps"]} rows={rows} />
        </Fig>
      );
    },
  },
];

// ---------------------------------------------------------------- bigger figures

function CacheFigure() {
  const chosen = cache.find((c) => c.label.startsWith("nomic +") && c.guard === 2)!;
  const pts = (k: "precision" | "recall") => chosen.curve_dev.filter((p) => p.t >= 0.7 && p[k] !== null).map((p) => [p.t, p[k] as number] as [number, number]);
  return (
    <>
      <Fig title="Dev set: precision and recall vs similarity threshold (chosen configuration)" cap={`nomic-embed-text, 'clustering: ' prefix, lexical guard ≤2 words. The threshold ${chosen.threshold} is the lowest with zero dev false hits. The test set was read once, after the choice.`}>
        <Legend items={[{ label: "precision", color: SERIES[0] }, { label: "recall", color: SERIES[1] }]} />
        <LineChart lines={[{ label: "precision", color: SERIES[0], points: pts("precision") }, { label: "recall", color: SERIES[1], points: pts("recall") }]}
          xLabel="threshold" xFormat={(v) => v.toFixed(2)} yFormat={pct} yMax={1} vline={{ x: chosen.threshold, label: `chosen ${chosen.threshold}` }} />
      </Fig>
      <Fig title="Held-out test set, every configuration tried" cap="Zero dev false hits became 3 of 33 on test, because 81 dev pairs were too few to expose them. Treat the threshold as provisional.">
        <table className="tbl">
          <thead><tr><th>embedding</th><th>guard</th><th className="num">threshold</th><th className="num">hit rate (recall)</th><th className="num">wrong hits</th></tr></thead>
          <tbody>
            {cache.map((c, i) => (
              <tr key={i} aria-selected={c === chosen}>
                <td className="mono">{c.label}</td><td>{c.guard ?? "none"}</td><td className="num">{c.threshold}</td>
                <td className="num">{pct(c.test.recall)}</td>
                <td className="num">{c.test.fp}/{c.test.tp + c.test.fp}{c.test.tp + c.test.fp ? ` (${pct(c.test.fp / (c.test.tp + c.test.fp))})` : ""}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Fig>
    </>
  );
}

function RoutingFigure() {
  const [strong, setStrong] = useState<keyof typeof routing>("mistral");
  const p = Object.entries(routing[strong]);
  const rows = (k: "acc" | "cost") => p.map(([name, v]) => ({
    label: name, emphasis: name === "heuristics",
    v: k === "acc" ? v.accuracy : v.rel_cost, lo: k === "acc" ? v.acc_lo : v.cost_lo, hi: k === "acc" ? v.acc_hi : v.cost_hi,
  }));
  return (
    <>
      <div className="seg" role="group" aria-label="Strong model">
        {(Object.keys(routing) as (keyof typeof routing)[]).map((k) => (
          <button key={k} aria-pressed={strong === k} onClick={() => setStrong(k)}>strong: {k}</button>
        ))}
      </div>
      <Fig title="Accuracy on 60 reference-graded prompts" cap="Dots are point estimates and lines are bootstrap intervals. On 60 prompts, every interval overlaps always-cheap's. The policies that score higher (self-consistency) cost more than always-strong.">
        <DotCI rows={rows("acc")} min={0.6} max={1} format={pct} refLine={{ v: routing[strong]["always cheap"].accuracy, label: "always cheap" }} />
      </Fig>
      <Fig title="Cost relative to always-strong" cap="'heuristics' (the free checks Sluice ships) costs about the same as always-cheap because it almost never fires.">
        <DotCI rows={rows("cost")} min={0} max={1.8} format={(v) => `${v.toFixed(1)}×`} color={SERIES[1]} />
        <DataTable head={["policy", "accuracy", "rel. cost", "escalated", "n"]} rows={p.map(([k, v]) => [k, pct(v.accuracy), v.rel_cost.toFixed(2), pct(v.escalated), v.n])} />
      </Fig>
    </>
  );
}

function Fig({ title, cap, children }: { title: string; cap?: string; children: ReactNode }) {
  return (
    <figure className="figure" style={{ margin: 0 }}>
      <h3>{title}</h3>
      {children}
      {cap && <figcaption className="cap">{cap}</figcaption>}
    </figure>
  );
}

// ---------------------------------------------------------------- panel

export default function Evidence({ selected }: { selected: string }) {
  const fileView = selected.startsWith("file:") ? selected.slice(5) : null;
  const cur = CLAIMS.find((c) => c.id === selected) ?? (fileView ? null : CLAIMS[0]);
  const counts = CLAIMS.reduce<Record<string, number>>((m, c) => ({ ...m, [c.verdict]: (m[c.verdict] ?? 0) + 1 }), {});
  return (
    <div className="split" style={{ gridTemplateColumns: "minmax(280px, 360px) minmax(0, 1fr)" }}>
      <section className="pane" aria-label="Claims">
        <div className="pane-h">
          <h2>Claims</h2><span className="grow" />
          <span>{CLAIMS.length} claims · {(counts.negative ?? 0) + (counts.caveat ?? 0) + (counts.loses ?? 0)} negative, caveated or lost</span>
        </div>
        <div className="pane-b">
          <ul className="claims">
            {CLAIMS.map((c) => {
              const V = VERDICT[c.verdict];
              return (
                <li key={c.id} className="claim" aria-selected={c === cur} tabIndex={0}
                  onClick={() => go("evidence", c.id)} onKeyDown={(e) => e.key === "Enter" && go("evidence", c.id)}>
                  <span className="row">
                    <span className="ttl">{c.title}</span>
                    <span className={`pill ${V.cls}`} style={{ flex: "none" }}><V.icon aria-hidden />{V.label}</span>
                  </span>
                  <span className="res">{c.claim.length > 110 ? c.claim.slice(0, 108) + "…" : c.claim}</span>
                </li>
              );
            })}
          </ul>
          <p style={{ padding: 12, fontSize: "var(--fs-xs)", color: "var(--ink-2)" }}>
            All numbers come from one 8 GB MacBook Air (M2), which also ran the load generator. Re-run the scripts before trusting them on other hardware.
          </p>
        </div>
      </section>
      <section className="pane" aria-label={cur?.title ?? fileView ?? ""}>
        <div className="pane-h">
          <h2 className="mono">{cur ? cur.files.map((f) => `bench/results/${f}`).join("  ·  ") : `bench/results/${fileView}`}</h2>
        </div>
        <div className="pane-b">
          {cur ? (
            <div className="ev">
              <div className="ev-head">
                <h1>{cur.title}</h1>
                <p className="claimline"><span className={`pill ${VERDICT[cur.verdict].cls}`}>{VERDICT[cur.verdict].label}</span>{cur.claim}</p>
                <div className="ev-src">
                  {cur.scripts.map((s) => <code key={s}>{s}</code>)}
                  <ArrowRight width={13} height={13} aria-hidden style={{ color: "var(--ink-3)" }} />
                  {cur.files.map((f) => results[f]
                    ? <a key={f} href={`#evidence/file:${f}`} onClick={(e) => { e.preventDefault(); go("evidence", `file:${f}`); }}><FileText width={12} height={12} aria-hidden /> {f}</a>
                    : <code key={f}>{f}</code>)}
                  {cur.adrs.map((a) => <a key={a} href={`#decisions/${a}`} onClick={(e) => { e.preventDefault(); go("decisions", a); }}><BookOpen width={12} height={12} aria-hidden /> ADR {a}</a>)}
                  {cur.lab && (
                    <button className="btn btn-sm" onClick={() => { try { localStorage.setItem("sluice.model", cur.lab!); } catch { /* ignore */ } go("lab"); }}>
                      <FlaskConical aria-hidden />Try it live: {cur.lab}
                    </button>
                  )}
                </div>
              </div>
              {cur.figure()}
            </div>
          ) : (
            <div className="ev">
              <a href="#evidence" onClick={(e) => { e.preventDefault(); history.back(); }} style={{ fontSize: "var(--fs-sm)" }}>← back</a>
              {fileView?.endsWith(".txt") ? <pre className="cmd">{results[fileView]}</pre> : <Markdown>{results[fileView!] ?? `No file ${fileView}`}</Markdown>}
            </div>
          )}
        </div>
      </section>
    </div>
  );
}
