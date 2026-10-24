import { X } from "lucide-react";
import type { Span } from "../lib/api";
import { ms, nsToMs } from "../lib/format";
import { ordered, stageOf, type StageId, type TraceView } from "../lib/trace";
import type { Step } from "./PipelineMap";

// Real time, not replay time: bars sit where the spans really were. The replay clock only
// decides which rows are revealed yet, so the map and the waterfall tell the story together.

export interface WaterfallProps {
  view: TraceView;
  steps: Step[];
  clock: number;
  hoverStage: StageId | null;
  selected: string | null;
  onHover: (span: Span | null) => void;
  onSelect: (span: Span) => void;
}

function revealStep(span: Span, view: TraceView, steps: Step[]): number {
  if (span === view.root) return 0;
  if (span.kind === "client") {
    const i = view.attempts.findIndex((a) => a.span === span);
    return steps.findIndex((s) => s.attempt === i);
  }
  return steps.findIndex((s) => s.stage === stageOf(span, view));
}

export default function Waterfall({ view, steps, clock, hoverStage, selected, onHover, onSelect }: WaterfallProps) {
  const rows = ordered(view);
  const total = Math.max(1, view.t1 - view.t0);
  const at = (ns: number) => ((ns - view.t0) / total) * 100;
  const ticks = [0, 0.25, 0.5, 0.75, 1];
  const events = view.root.events;
  // the replay's time cursor: where the shared clock is, drawn across every row while it runs
  const cursor = clock > 0 && clock < steps.length ? at(steps[clock - 1].ns) : null;

  return (
    <div className="wf" role="table" aria-label="Span waterfall">
      <div className="wf-scale" role="row">
        <div style={{ padding: "3px 8px", fontSize: "var(--fs-xs)", color: "var(--ink-2)" }}>span</div>
        <div style={{ position: "relative" }}>
          {ticks.map((t) => (
            <span key={t} className="tick" style={{ left: `${t * 100}%`, ...(t === 1 ? { transform: "translateX(-100%)", paddingRight: 6 } : {}) }}>
              {ms(nsToMs(total) * t)}
            </span>
          ))}
        </div>
      </div>
      {rows.map((s) => {
        const depth = view.depth.get(s.span_id) ?? 0;
        const left = at(s.start_ns);
        const width = Math.max(0.3, at(s.end_ns) - left);
        const shown = revealStep(s, view, steps) < clock;
        const err = s.status === "error";
        const hov = hoverStage !== null && stageOf(s, view) === hoverStage && s !== view.root;
        const dur = nsToMs(s.end_ns - s.start_ns);
        return (
          <div
            key={s.span_id}
            role="row"
            className={`wf-row ${hov ? "hover" : ""}`}
            aria-selected={selected === s.span_id}
            style={{ opacity: shown ? 1 : 0.28, transition: "opacity 220ms var(--ease)", cursor: "pointer" }}
            tabIndex={0}
            onMouseEnter={() => onHover(s)}
            onMouseLeave={() => onHover(null)}
            onFocus={() => onHover(s)}
            onBlur={() => onHover(null)}
            onClick={() => onSelect(s)}
            onKeyDown={(e) => {
              if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onSelect(s); }
              const to = e.key === "ArrowDown" || e.key === "j" ? e.currentTarget.nextElementSibling
                : e.key === "ArrowUp" || e.key === "k" ? e.currentTarget.previousElementSibling : null;
              if (to instanceof HTMLElement && to.tabIndex === 0) { e.preventDefault(); to.focus(); }
            }}
          >
            <div className="wf-name" role="cell" style={{ paddingLeft: 8 + depth * 14 }}>
              {err ? <X className="v-fail" aria-label="error" strokeWidth={2.6} /> : <i className={`kd ${s.kind}`} aria-hidden />}
              <span title={s.name}>{s.name}</span>
            </div>
            <div className="wf-track" role="cell">
              <div className={`wf-bar ${s.kind} ${err ? "error" : ""}`} style={{ left: `${left}%`, width: `${width}%` }} />
              <span className="wf-dur" style={
                left + width <= 78 ? { left: `calc(${left + width}% + 5px)` }
                  : left > 22 ? { right: `${100 - left + 1}%` }
                  : { left: `calc(${left}% + 6px)`, color: "#fff" } /* a bar that spans the track: label inside it */}>
                {ms(dur)}
                {s.attributes["error.type"] !== undefined ? ` · ${s.attributes["error.type"]}` : ""}
              </span>
              {cursor !== null && <i className="wf-cursor" style={{ left: `${cursor}%` }} aria-hidden />}
              {s === view.root && events.map((e, i) => (
                <i
                  key={i}
                  className={`wf-ev ${e.name === "sluice.fallback" ? "fallback" : e.name.includes("cache") ? "hit" : ""}`}
                  style={{ left: `${at(e.ts_ns)}%` }}
                  title={`${e.name} ${JSON.stringify(e.attributes)}`}
                />
              ))}
            </div>
          </div>
        );
      })}
    </div>
  );
}
