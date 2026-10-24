import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Eye, EyeOff, Play, RotateCcw, Send } from "lucide-react";
import { api, ApiError, send, type BreakerState, type Overview, type SendResult, type Span, type Trace, type TraceRoot } from "../lib/api";
import { ago, ms, nsToMs } from "../lib/format";
import { go } from "../lib/nav";
import { describeRoute, providerBehaviour, wantsStream } from "../lib/routes";
import { analyse, stageOf, type StageId, type Verdict } from "../lib/trace";
import PipelineMap, { replaySteps } from "../components/PipelineMap";
import Waterfall from "../components/Waterfall";
import Inspector from "../components/Inspector";
import { VerdictIcon } from "../components/marks";
import { ApiProblem, Empty, SkeletonRows, usePoll } from "../components/state";

interface Local {
  localId: string;
  traceId: string | null;
  model: string;
  startedAt: number;
  pending: boolean;
  text: string;
  result: SendResult | null;
  error: string | null;
}

// One shared clock for the map and the waterfall. Each step lasts in proportion to the real time
// until the next one, with a floor so sub-millisecond stages (auth, limits) are still seen.
const STEP_FLOOR_MS = 170, REPLAY_MS = 2200;

const store = {
  get: (k: string) => { try { return localStorage.getItem(k) ?? ""; } catch { return ""; } },
  set: (k: string, v: string) => { try { localStorage.setItem(k, v); } catch { /* private mode */ } },
};

export default function Lab({ overview, health, traceArg }: {
  overview: { data: Overview | null; error: ApiError | null; loading: boolean };
  health: Record<string, BreakerState> | null;
  traceArg: string;
}) {
  const o = overview.data;
  const routeNames = useMemo(() => Object.keys(o?.routes ?? {}), [o]);
  const [model, setModel] = useState(() => store.get("sluice.model"));
  const [key, setKey] = useState(() => store.get("sluice.key"));
  const [showKey, setShowKey] = useState(false);
  const [prompt, setPrompt] = useState("What is a sluice, in one sentence?");
  const [stream, setStream] = useState(false);
  const [noCache, setNoCache] = useState(false);
  const [json, setJson] = useState(false);
  const [repeat, setRepeat] = useState(1);
  const [locals, setLocals] = useState<Local[]>([]);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (o && !o.routes[model]) setModel(o.routes["lab-failover"] ? "lab-failover" : routeNames[0] ?? "");
  }, [o, model, routeNames]);
  useEffect(() => store.set("sluice.model", model), [model]);
  useEffect(() => store.set("sluice.key", key), [key]);
  const route = o?.routes[model] ?? null;
  useEffect(() => {
    if (route && o && wantsStream(route, o)) setStream(true);
  }, [route, o]);

  const traces = usePoll(() => api.traces(60), 2500);
  const selectedId = traceArg || null;

  // ---- the selected trace: spans + ledger row, refetched until the ledger row lands
  const [trace, setTrace] = useState<Trace | null>(null);
  const [traceErr, setTraceErr] = useState<string | null>(null);
  useEffect(() => {
    setTrace(null);
    setTraceErr(null);
    if (!selectedId) return;
    let stop = false, tries = 0;
    const load = async () => {
      try {
        const t = await api.trace(selectedId);
        if (stop) return;
        setTrace(t);
        if (!t.ledger && tries++ < 4) setTimeout(load, 600); // ledger flushes once a second
      } catch (e) {
        if (stop) return;
        if (e instanceof ApiError && e.status === 404 && tries++ < 6) setTimeout(load, 400);
        else setTraceErr(e instanceof ApiError && e.status === 404
          ? "This trace is no longer held: the gateway keeps only the most recent ones in memory."
          : String((e as Error).message));
      }
    };
    load();
    return () => { stop = true; };
  }, [selectedId]);
  const view = useMemo(() => (trace ? analyse(trace.spans, o?.routes) : null), [trace, o]);
  const steps = useMemo(() => replaySteps(view), [view]);
  const viewRouteName = view ? String(view.root.attributes["gen_ai.request.model"] ?? "") : model;
  const mapRoute = o?.routes[viewRouteName] ?? route;

  // ---- replay clock
  const [clock, setClock] = useState(0);
  const timer = useRef<number>();
  const replay = useCallback(() => {
    clearTimeout(timer.current);
    if (matchMedia("(prefers-reduced-motion: reduce)").matches) { setClock(steps.length); return; }
    const total = Math.max(1, steps[steps.length - 1].ns - steps[0].ns);
    const tick = (c: number) => {
      setClock(c);
      if (c >= steps.length) return;
      const gap = steps[c].ns - steps[c - 1].ns;
      timer.current = window.setTimeout(() => tick(c + 1), STEP_FLOOR_MS + (gap / total) * REPLAY_MS);
    };
    tick(1);
  }, [steps]);
  const replayedFor = useRef<string | null>(null);
  useEffect(() => {
    if (!view || !trace) { setClock(0); return; }
    // replay once per trace, the first time it's shown; later visits show the end state
    if (replayedFor.current !== trace.trace_id) { replayedFor.current = trace.trace_id; replay(); }
    return () => clearTimeout(timer.current);
  }, [view, trace, replay]);

  // ---- hover linking between map and waterfall
  const [hoverStage, setHoverStage] = useState<StageId | null>(null);
  const [hoverAttempt, setHoverAttempt] = useState<number | null>(null);
  const [pickedSpan, setPickedSpan] = useState<Span | null>(null);
  useEffect(() => setPickedSpan(null), [selectedId]);
  const onWfHover = (s: Span | null) => {
    setHoverStage(s && view ? (s === view.root ? null : stageOf(s, view)) : null);
    setHoverAttempt(s && view ? view.attempts.findIndex((a) => a.span === s) : null);
  };
  const onPickStage = (st: StageId) => {
    if (!view) return;
    const s = st === "executor" ? view.attempts.at(-1)?.span : st === "response" || st === "client" || st === "quality" ? view.root : view.stages.find((x) => x.id === st)?.span;
    if (s) setPickedSpan(s);
  };

  // ---- sending
  async function fire() {
    if (!route) return;
    setBusy(true);
    try {
      for (let i = 0; i < repeat; i++) {
        const localId = Math.random().toString(36).slice(2);
        const entry: Local = { localId, traceId: null, model, startedAt: Date.now(), pending: true, text: "", result: null, error: null };
        setLocals((l) => [entry, ...l].slice(0, 100));
        const patch = (p: Partial<Local>) => setLocals((l) => l.map((x) => (x.localId === localId ? { ...x, ...p } : x)));
        try {
          const res = await send({ key, model, prompt, stream, noCache, json, maxTokens: null }, (text) => patch({ text }));
          patch({ pending: false, result: res, traceId: res.traceId, text: res.content });
          if (i === repeat - 1 && res.traceId) go("lab", res.traceId);
        } catch (e) {
          patch({ pending: false, error: (e as Error).message });
          break;
        }
      }
      traces.reload();
    } finally {
      setBusy(false);
    }
  }

  // ---- the request list: this session's sends, plus what the gateway has seen
  const localByTrace = new Map(locals.filter((l) => l.traceId).map((l) => [l.traceId!, l]));
  const pendingLocals = locals.filter((l) => !l.traceId);
  const rows: { id: string; root?: TraceRoot; local?: Local }[] = [
    ...pendingLocals.map((l) => ({ id: l.localId, local: l })),
    // chat requests only: a trace whose root isn't a server span isn't a request to the gateway
    ...(traces.data ?? []).filter((r) => r.kind === "server").map((r) => ({ id: r.trace_id, root: r, local: localByTrace.get(r.trace_id) })),
  ];
  const selLocal = selectedId ? localByTrace.get(selectedId) ?? null : pendingLocals[0] ?? null;

  if (overview.error && !o) {
    return <div className="page"><div className="page-in"><ApiProblem error={overview.error} /></div></div>;
  }

  return (
    <div className="lab">
      {/* ------------------------------------------------ composer + requests */}
      <section className="pane" aria-label="Composer and requests">
        <div className="pane-h"><h2>Send a request</h2></div>
        <form className="composer" onSubmit={(e) => { e.preventDefault(); fire(); }}>
          <label className="field">
            <span>Route</span>
            <select className="select mono" value={model} onChange={(e) => setModel(e.target.value)} disabled={!o}>
              {routeNames.map((r) => <option key={r} value={r}>{r}</option>)}
            </select>
            {route && o && (
              <small title={route.chain.map((t) => `${t.provider}: ${providerBehaviour(o.providers[t.provider])}`).join("\n")}>
                {describeRoute(route, o)}
              </small>
            )}
          </label>
          <label className="field">
            <span>Virtual key</span>
            <div style={{ display: "flex", gap: 4 }}>
              <input className="input mono" type={showKey ? "text" : "password"} value={key} onChange={(e) => setKey(e.target.value.trim())}
                placeholder="sk-sluice-…" autoComplete="off" spellCheck={false} aria-invalid={!key && o?.auth.enabled ? true : undefined} />
              <button type="button" className="btn btn-ghost btn-sm" style={{ height: 30 }} onClick={() => setShowKey((v) => !v)} aria-label={showKey ? "Hide key" : "Show key"}>
                {showKey ? <EyeOff /> : <Eye />}
              </button>
            </div>
            {!key && o?.auth.enabled && (
              <small>No key yet? <code>python -m sluice.admin create-key --name lab --rpm 120</code></small>
            )}
          </label>
          <label className="field">
            <span>Prompt</span>
            <textarea className="textarea" value={prompt} onChange={(e) => setPrompt(e.target.value)} rows={3} />
          </label>
          <div className="row">
            <label className="check"><input type="checkbox" checked={stream} onChange={(e) => setStream(e.target.checked)} />Stream</label>
            <label className="check" title="Sends Cache-Control: no-cache"><input type="checkbox" checked={noCache} onChange={(e) => setNoCache(e.target.checked)} />Skip cache</label>
            <label className="check" title="response_format: json_object"><input type="checkbox" checked={json} onChange={(e) => setJson(e.target.checked)} />JSON</label>
          </div>
          <div className="send-row">
            <button className="btn btn-primary" type="submit" disabled={!route || busy || !prompt.trim()}>
              <Send aria-hidden />{busy ? "Sending…" : repeat > 1 ? `Send ${repeat}×` : "Send"}
            </button>
            <select className="select" value={repeat} onChange={(e) => setRepeat(Number(e.target.value))} aria-label="Repeat" title="Send several in a row: opens breakers, trips rate limits">
              {[1, 5, 20].map((n) => <option key={n} value={n}>{n}×</option>)}
            </select>
          </div>
        </form>
        <div className="pane-h">
          <h2>Requests</h2>
          <span className="grow" />
          <span>{traces.data ? `${traces.data.length} held` : ""}</span>
        </div>
        <div className="pane-b">
          {traces.error && !traces.data ? <div style={{ padding: 10 }}><ApiProblem error={traces.error} /></div>
            : traces.loading && !traces.data ? <SkeletonRows />
            : rows.length === 0 ? <Empty title="No requests yet">Pick a lab route and press Send. Every request the gateway handles shows up here, whoever sent it.</Empty>
            : (
              <ul className="reqlist">
                {rows.map(({ id, root, local }) => {
                  const status = root ? Number(root.attributes["http.response.status_code"] ?? 0) : local?.result?.status ?? 0;
                  const outcome = root ? String(root.attributes["sluice.outcome"] ?? "") : "";
                  const v: Verdict = local?.pending ? "idle" : status >= 400 || outcome === "stream_interrupted" || local?.error ? "fail"
                    : Number(root?.attributes["sluice.fallbacks"] ?? 0) > 0 || outcome === "resumed" || root?.events.some((e) => e.name === "sluice.escalate") ? "warn"
                    : root?.attributes["sluice.cache.result"] === "hit" ? "hit" : "pass";
                  const prov = root ? String(root.attributes["sluice.provider"] ?? "") : "";
                  return (
                    <li key={id} className="req" aria-selected={root ? selectedId === root.trace_id : !selectedId && local === pendingLocals[0]}
                      onClick={() => root && go("lab", root.trace_id)} tabIndex={0}
                      onKeyDown={(e) => e.key === "Enter" && root && go("lab", root.trace_id)}>
                      {local?.pending ? <span className="skel" style={{ width: 12, height: 12, borderRadius: 6 }} /> : <VerdictIcon v={v} />}
                      <span className="m">{root ? String(root.attributes["gen_ai.request.model"]) : local!.model}</span>
                      <span className="r">{root ? ms(nsToMs(root.end_ns - root.start_ns)) : local?.pending ? "…" : ""}</span>
                      <span className="meta">
                        {local?.error ? local.error
                          : root ? `${status} · ${prov || "—"} · ${root.attributes["sluice.attempts"] ?? 0} att · ${ago(root.end_ns / 1e6)}${local ? " · you" : ""}`
                          : "in flight"}
                      </span>
                    </li>
                  );
                })}
              </ul>
            )}
        </div>
      </section>

      {/* ------------------------------------------------ map + waterfall */}
      <section className="pane centre" aria-label="Pipeline and spans">
        <div className="mapwrap">
          <div className="pane-h">
            <h2>Pipeline</h2>
            <span className="mono">{viewRouteName}</span>
            <span className="grow" />
            {view && (
              <button className="btn btn-ghost btn-sm" onClick={replay} title="Replay this request through the pipeline">
                {clock >= steps.length ? <RotateCcw /> : <Play />}Replay
              </button>
            )}
          </div>
          <div style={{ padding: "6px 10px 4px" }}>
            <PipelineMap routeName={viewRouteName} route={mapRoute} overview={o} health={health} view={view} clock={clock}
              hover={hoverStage} hoverAttempt={hoverAttempt} onHover={setHoverStage} onPick={onPickStage} />
          </div>
        </div>
        <div className="pane" style={{ minHeight: 0 }}>
          <div className="pane-h">
            <h2>Spans</h2>
            {trace && <span className="mono" title={trace.trace_id}>trace {trace.trace_id.slice(0, 12)}…</span>}
            <span className="grow" />
            <span className="wf-legend" aria-hidden>
              <span><i style={{ background: "#57606a" }} />request</span>
              <span><i style={{ background: "var(--accent)" }} />upstream</span>
              <span><i style={{ background: "var(--bad)" }} />error</span>
              <span><i style={{ background: "var(--bad)", width: 2, height: 10 }} />fallback</span>
            </span>
          </div>
          <div className="pane-b">
            {traceErr ? <Empty title="Trace unavailable">{traceErr}</Empty>
              : !selectedId ? <Empty title="Select a request">Its spans appear here at their real timings: auth, limits, cache, then every upstream attempt.</Empty>
              : !view ? <SkeletonRows n={5} />
              : <Waterfall view={view} steps={steps} clock={clock} hoverStage={hoverStage} selected={pickedSpan?.span_id ?? null}
                  onHover={onWfHover} onSelect={setPickedSpan} />}
          </div>
        </div>
      </section>

      {/* ------------------------------------------------ inspector */}
      <section className="pane inspector" aria-label="Inspector">
        <div className="pane-h"><h2>Inspector</h2>
          {view && <><span className="grow" /><span className={`mark v-${view.status >= 400 ? "fail" : "pass"}`}><span className="t num">{view.status}</span></span></>}
        </div>
        <Inspector view={view} ledger={trace?.ledger ?? null}
          local={selLocal ? { result: selLocal.result, text: selLocal.text, pending: selLocal.pending } : null} span={pickedSpan} />
      </section>
    </div>
  );
}
