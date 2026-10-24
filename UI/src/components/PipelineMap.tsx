import type { BreakerState, Overview, Route, Target } from "../lib/api";
import { STAGE_LABEL, type StageId, type TraceView, type Verdict } from "../lib/trace";

// The gateway as a left-to-right map: client → auth → limits → cache → executor → quality →
// response. The executor box holds one lane per target in the route's chain (and the escalate
// route's chain below it), each with its live breaker lamp and the attempts this request made.

export interface Step {
  stage: StageId;
  attempt?: number; // index into view.attempts
  ns: number; // when the request reached this step (span start, or the event's timestamp)
}

/** The order the request touched things in: the replay clock walks these one by one. */
export function replaySteps(v: TraceView | null): Step[] {
  if (!v) return [];
  const stage = (id: StageId) => v.stages.find((s) => s.id === id)!;
  const out: Step[] = [{ stage: "client", ns: v.t0 }];
  for (const id of ["auth", "limits", "cache"] as StageId[]) {
    const st = stage(id);
    if (st.verdict !== "idle") out.push({ stage: id, ns: st.span?.start_ns ?? out[out.length - 1].ns });
  }
  v.attempts.forEach((a, i) => {
    if (a.escalation && !out.some((s) => s.stage === "quality")) out.push({ stage: "quality", ns: v.escalate?.ts_ns ?? a.span.start_ns });
    out.push({ stage: "executor", attempt: i, ns: a.span.start_ns });
  });
  if (stage("quality").verdict !== "idle" && !out.some((s) => s.stage === "quality")) {
    const last = v.attempts.at(-1);
    out.push({ stage: "quality", ns: last ? last.span.end_ns : out[out.length - 1].ns });
  }
  out.push({ stage: "response", ns: v.root.end_ns });
  return out;
}

const NODE_W = 104, NODE_H = 62, TOP = 46, MID = TOP + NODE_H / 2;
const X: Record<Exclude<StageId, "executor">, number> = { client: 4, auth: 122, limits: 240, cache: 358, quality: 778, response: 896 };
const EX_X = 476, EX_W = 288, LANE_H = 34, LANE_TOP = 40;

interface Lane { t: Target; escalation: boolean; y: number }

function lanesFor(route: Route | null): { lanes: Lane[]; exH: number } {
  if (!route) return { lanes: [], exH: NODE_H };
  const lanes: Lane[] = [];
  let y = TOP - 12 + LANE_TOP;
  route.chain.forEach((t) => { lanes.push({ t, escalation: false, y }); y += LANE_H; });
  if (route.escalate) {
    y += 18;
    route.escalate.chain.forEach((t) => { lanes.push({ t, escalation: true, y }); y += LANE_H; });
  }
  return { lanes, exH: Math.max(NODE_H + 24, y - (TOP - 12) - LANE_H / 2 + 10) };
}

function Lamp({ x, y, state }: { x: number; y: number; state?: BreakerState }) {
  if (!state) return null;
  if (state === "open") {
    const r = 5.5, pts = Array.from({ length: 8 }, (_, i) => {
      const a = Math.PI / 8 + (i * Math.PI) / 4;
      return `${x + r * Math.cos(a)},${y + r * Math.sin(a)}`;
    }).join(" ");
    return <polygon points={pts} fill="var(--bad)"><title>breaker open: skipped until cooldown</title></polygon>;
  }
  if (state === "half_open") {
    return (
      <g>
        <title>breaker half-open: one probe allowed</title>
        <circle cx={x} cy={y} r={5} fill="#fff" stroke="var(--warn-fill)" strokeWidth={1.6} />
        <path d={`M${x},${y - 5} A5,5 0 0 1 ${x},${y + 5} Z`} fill="var(--warn-fill)" />
      </g>
    );
  }
  return <circle cx={x} cy={y} r={5} fill="var(--ok)"><title>breaker closed: traffic flows</title></circle>;
}

export interface MapProps {
  routeName: string;
  route: Route | null;
  overview: Overview | null;
  health: Record<string, BreakerState> | null;
  view: TraceView | null;
  clock: number; // steps revealed so far
  hover: StageId | null;
  hoverAttempt: number | null;
  onHover: (s: StageId | null) => void;
  onPick: (s: StageId) => void;
}

export default function PipelineMap(p: MapProps) {
  const { view, clock } = p;
  const steps = replaySteps(view);
  const { lanes, exH } = lanesFor(p.route);
  const exTop = TOP - 12;
  const H = Math.max(TOP + NODE_H + 22, exTop + exH + 18);
  const stepOf = (id: StageId) => steps.findIndex((s) => s.stage === id);
  const revealed = (id: StageId) => !!view && stepOf(id) >= 0 && stepOf(id) < clock;
  const verdict = (id: StageId): Verdict | null => (revealed(id) ? view!.stages.find((s) => s.id === id)!.verdict : null);
  const note = (id: StageId) => (revealed(id) ? view!.stages.find((s) => s.id === id)!.note : "");
  const hit = verdict("cache") === "hit";

  // where the pulse sits: the latest revealed step
  const last = steps[Math.min(clock, steps.length) - 1];
  let px = X.client + NODE_W / 2, py = MID;
  if (last) {
    if (last.stage === "executor" && last.attempt !== undefined) {
      const a = view!.attempts[last.attempt];
      const lane = lanes.find((l) => l.t.provider === a.provider && l.escalation === a.escalation) ?? lanes[0];
      px = EX_X + 160 + attemptsOn(view!, lane).indexOf(last.attempt) * 22;
      py = lane ? lane.y : MID;
    } else if (last.stage !== "executor") {
      px = X[last.stage] + NODE_W / 2;
      py = MID;
    }
  }
  const running = !!view && clock < steps.length;

  const node = (id: Exclude<StageId, "executor">) => {
    const v = verdict(id);
    const cls = ["node", v ? (v === "pass" ? "on" : `v-${v}`) : "", p.hover === id ? "hover" : ""].join(" ");
    return (
      <g key={id} onMouseEnter={() => p.onHover(id)} onMouseLeave={() => p.onHover(null)} onClick={() => p.onPick(id)} style={{ cursor: "pointer" }}>
        <rect className={cls} x={X[id]} y={TOP} width={NODE_W} height={NODE_H} rx={6} />
        <text className="lbl" x={X[id] + 10} y={TOP + 25}>{STAGE_LABEL[id]}</text>
        <text className="sub" x={X[id] + 10} y={TOP + 48}>{clip(note(id), 10)}<title>{note(id)}</title></text>
        {v && v !== "pass" && <VerdictGlyph v={v} x={X[id] + NODE_W - 14} y={TOP + 17} />}
      </g>
    );
  };

  const wire = (from: number, to: number, on: boolean, cls = "") => (
    <line className={`wire ${on ? "on" : ""} ${cls}`} x1={from} y1={MID} x2={to} y2={MID} />
  );
  const failedAt = view?.stages.find((s) => s.verdict === "fail" && revealed(s.id))?.id;
  const ex = verdict("executor");

  return (
    <svg className="map" viewBox={`0 0 1000 ${H}`} role="img" aria-label={`Pipeline for route ${p.routeName}`}>
      {/* wires */}
      {wire(X.client + NODE_W, X.auth, revealed("auth"), failedAt === "auth" ? "fail" : "")}
      {wire(X.auth + NODE_W, X.limits, revealed("limits"), failedAt === "limits" ? "fail" : "")}
      {wire(X.limits + NODE_W, X.cache, revealed("cache"))}
      {wire(X.cache + NODE_W, EX_X, revealed("cache") && !hit && clock > stepOf("cache") + 1)}
      {wire(EX_X + EX_W, X.quality, revealed("quality") || (revealed("response") && !hit && ex !== "fail"))}
      {wire(X.quality + NODE_W, X.response, revealed("response") && !hit, failedAt === "response" && !hit ? "fail" : "")}
      {/* cache hit: answered from the cache, the executor is never called */}
      <path
        className={`wire ${hit && revealed("response") ? "hit" : ""}`}
        d={`M${X.cache + NODE_W / 2},${TOP} C${X.cache + 60},${4} ${X.response - 20},${4} ${X.response + NODE_W / 2},${TOP}`}
        strokeDasharray={hit ? undefined : "3 4"}
        opacity={hit ? 1 : 0.7}
      />
      <text className="sub" x={(X.cache + X.response) / 2 + 40} y={15} textAnchor="middle">cache hit bypass</text>

      {(["client", "auth", "limits", "cache", "quality", "response"] as const).map(node)}

      {/* executor */}
      <g onMouseEnter={() => p.onHover("executor")} onMouseLeave={() => p.onHover(null)} onClick={() => p.onPick("executor")} style={{ cursor: "pointer" }}>
        <rect className={["node", ex ? (ex === "pass" ? "on" : `v-${ex}`) : "", p.hover === "executor" ? "hover" : ""].join(" ")} x={EX_X} y={exTop} width={EX_W} height={exH} rx={6} />
        <text className="lbl" x={EX_X + 10} y={exTop + 22}>Executor</text>
        <text className="sub" x={EX_X + EX_W - 10} y={exTop + 22} textAnchor="end">{clip(note("executor") || (p.route ? `deadline ${p.route.deadline_s}s` : ""), 19)}<title>{note("executor")}</title></text>
        {lanes.map((l, i) => {
          const served = view && revealed("response") && view.provider === l.t.provider && !hit;
          const mine = view ? attemptsOn(view, l) : [];
          const breaker = p.health?.[l.t.provider] ?? p.overview?.providers[l.t.provider]?.breaker?.state;
          return (
            <g key={i}>
              {l.escalation && i > 0 && !lanes[i - 1].escalation && (
                <text className="sub" x={EX_X + 10} y={l.y - LANE_H / 2 - 3}>escalate route ({p.route?.escalate_on.length} checks)</text>
              )}
              <rect className="lane" x={EX_X + 8} y={l.y - 13} width={EX_W - 16} height={26} rx={3}
                style={served ? { fill: "var(--accent-soft)", stroke: "var(--accent-line)" } : undefined} />
              <Lamp x={EX_X + 20} y={l.y} state={breaker} />
              <text x={EX_X + 32} y={l.y + 4.5} style={{ font: "600 14px var(--mono)", fill: "var(--ink)" }}>
                {clip(l.t.provider, 12)}<title>{`${l.t.provider} → ${l.t.upstream_model} · timeout ${l.t.timeout_s}s · retries ${l.t.retries}`}</title>
              </text>
              {mine.map((ai, k) => {
                const a = view!.attempts[ai];
                const shown = steps.findIndex((s) => s.attempt === ai) < clock;
                const pa = p.hoverAttempt === ai;
                return (
                  <g key={ai} opacity={shown ? 1 : 0} style={{ transition: "opacity 200ms" }}>
                    <rect className="att" x={EX_X + 150 + k * 22} y={l.y - 9} width={18} height={18} rx={3}
                      fill={a.ok ? "var(--ok)" : "var(--bad)"} strokeWidth={pa ? 2.5 : 1.5} stroke={pa ? "var(--ink)" : "#fff"} />
                    <path d={a.ok ? `M${EX_X + 154 + k * 22},${l.y} l3,3 l6,-6` : `M${EX_X + 155 + k * 22},${l.y - 4} l8,8 m0,-8 l-8,8`}
                      stroke="#fff" strokeWidth={1.8} fill="none" strokeLinecap="round" strokeLinejoin="round" />
                    <title>{`attempt ${a.index + 1} on ${a.provider}: ${a.ok ? "ok" : a.errorType ?? "error"}`}</title>
                  </g>
                );
              })}
              {mine.length > 0 && !view!.attempts[mine[mine.length - 1]].ok && i < lanes.length - 1 &&
                steps.findIndex((s) => s.attempt === mine[mine.length - 1]) < clock && (
                <text className="sub" x={EX_X + EX_W - 14} y={l.y + 4} textAnchor="end" style={{ fill: "var(--bad)" }}>
                  {fallbackReason(view!, l.t.provider)}<title>fell over to the next target</title>
                </text>
              )}
              {served && (
                <text className="sub" x={EX_X + EX_W - 14} y={l.y + 4} textAnchor="end" style={{ fill: "var(--accent-ink)", fontWeight: 600 }}>served</text>
              )}
            </g>
          );
        })}
        {!p.route && <text className="sub" x={EX_X + 10} y={exTop + 44}>pick a route to see its chain</text>}
      </g>

      {/* the request */}
      {view && (
        <circle
          className="pulse" r={running ? 6 : 0} cx={0} cy={0}
          style={{ transform: `translate(${px}px, ${py}px)`, transition: "transform 240ms var(--ease), r 200ms" }}
        />
      )}
    </svg>
  );
}

function attemptsOn(v: TraceView, l: Lane): number[] {
  return v.attempts.flatMap((a, i) => (a.provider === l.t.provider && a.escalation === l.escalation ? [i] : []));
}

function fallbackReason(v: TraceView, provider: string): string {
  const f = v.fallbacks.find((e) => e.attributes["sluice.provider"] === provider);
  return f ? String(f.attributes["error.type"] ?? "failed") : "";
}

function VerdictGlyph({ v, x, y }: { v: Verdict; x: number; y: number }) {
  const color = { fail: "var(--bad)", warn: "var(--warn-fill)", hit: "var(--hit)", skip: "var(--idle)", idle: "var(--idle)", pass: "var(--ok)" }[v];
  const d = {
    fail: `M${x - 4},${y - 4} l8,8 m0,-8 l-8,8`,
    warn: `M${x - 4},${y} a4,4 0 1 1 1.5,3.2 M${x - 4},${y} l-1,-3 m1,3 l3,-0.6`,
    hit: `M${x + 1},${y - 5} l-4,6 h4 l-1,5 l5,-7 h-4 z`,
    skip: `M${x - 4},${y} h8`,
    idle: `M${x - 4},${y} h8`,
    pass: `M${x - 4},${y} l3,3 l5,-6`,
  }[v];
  return <path d={d} stroke={color} strokeWidth={2} fill={v === "hit" ? color : "none"} strokeLinecap="round" strokeLinejoin="round" />;
}

const clip = (s: string, n: number) => (s.length > n ? s.slice(0, n - 1) + "…" : s);
