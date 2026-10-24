import { useLayoutEffect, useRef, useState, type ReactNode } from "react";

// A small chart kit for the evidence panel. Thin marks, 2px surface gaps between fills,
// recessive grid, one y-axis per chart, values in ink (never in the series colour), a hover
// tooltip on every mark, and a data table under every figure.

export const SERIES = ["var(--series-1)", "var(--series-2)", "var(--series-3)"];

/** Charts draw at their real pixel width (viewBox = width), so 11px text stays 11px on a phone. */
function useWidth(fallback = 720) {
  const ref = useRef<HTMLDivElement>(null);
  const [w, setW] = useState(fallback);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const ro = new ResizeObserver(([e]) => setW(Math.max(280, Math.round(e.contentRect.width))));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return { ref, w };
}

/** Fit a label into `px` of 11px text (about 6.2px a character). */
/** A label that doesn't fit goes onto two lines (split at " (" or a space), never cut mid-meaning. */
const wrap2 = (t: string, px: number): string[] => {
  const n = Math.floor(px / 6.2);
  if (t.length <= n) return [t];
  const at = t.indexOf(" (") > 0 ? t.indexOf(" (") : t.lastIndexOf(" ", n);
  return at > 0 ? [t.slice(0, at), fit(t.slice(at + 1), px)] : [fit(t, px)];
};

const fit = (t: string, px: number) => {
  const n = Math.floor(px / 6.2);
  return t.length > n ? t.slice(0, Math.max(1, n - 1)) + "…" : t;
};

interface Tip { x: number; y: number; body: ReactNode }

function useTip() {
  const [tip, setTip] = useState<Tip | null>(null);
  const on = (body: ReactNode) => ({
    onMouseMove: (e: React.MouseEvent) => setTip({ x: e.clientX, y: e.clientY, body }),
    onMouseLeave: () => setTip(null),
  });
  const node = tip && (
    <div className="tip" style={{ left: Math.min(tip.x + 12, innerWidth - 290), top: tip.y + 14 }}>{tip.body}</div>
  );
  return { on, node };
}

export function Legend({ items }: { items: { label: string; color: string; dot?: boolean }[] }) {
  return (
    <div className="legend">
      {items.map((i) => (
        <span key={i.label}><i className={i.dot ? "dot" : ""} style={{ background: i.color }} />{i.label}</span>
      ))}
    </div>
  );
}

const niceMax = (v: number) => {
  if (v <= 0) return 1;
  const p = 10 ** Math.floor(Math.log10(v));
  return [1, 2, 2.5, 5, 10].map((m) => m * p).find((m) => m >= v)!;
};

// ---------------------------------------------------------------------- horizontal bars

export interface BarRow { label: string; values: number[]; note?: string }

/** Horizontal bars: grouped (one bar per series) or stacked. One shared x-axis. */
export function HBars({ rows, series, stacked, max, unit = "", format = (v: number) => `${v}${unit}`, marker, labelW = 190 }: {
  rows: BarRow[]; series: string[]; stacked?: boolean; max?: number; unit?: string;
  format?: (v: number) => string; marker?: (row: BarRow) => number | null; labelW?: number;
}) {
  const { on, node } = useTip();
  const { ref, w: W } = useWidth();
  labelW = Math.min(labelW, Math.round(W * 0.36));
  const bh = 12, gap = 2, groupGap = 12;
  const per = stacked ? bh : series.length * bh + (series.length - 1) * gap;
  const H = rows.length * (per + groupGap) + 22;
  const top = Math.max(...rows.map((r) => (stacked ? r.values.reduce((a, b) => a + b, 0) : Math.max(...r.values))));
  const xMax = max ?? niceMax(top);
  const x = (v: number) => labelW + (v / xMax) * (W - labelW - 60);
  const ticks = [0, 0.25, 0.5, 0.75, 1].map((t) => t * xMax);
  return (
    <div ref={ref}>
      <svg className="chart" viewBox={`0 0 ${W} ${H}`} role="img">
        {ticks.map((t) => (
          <g key={t}>
            <line className="gl" x1={x(t)} x2={x(t)} y1={0} y2={H - 18} />
            <text x={x(t)} y={H - 4} textAnchor="middle">{format(t)}</text>
          </g>
        ))}
        {rows.map((r, i) => {
          const y0 = i * (per + groupGap) + 4;
          let acc = 0;
          const m = marker?.(r);
          return (
            <g key={r.label}>
              <text x={labelW - 10} y={y0 + per / 2 + 4} textAnchor="end" style={{ fill: "var(--ink)" }}>{fit(r.label, labelW - 12)}<title>{r.label}</title></text>
              {r.values.map((v, s) => {
                const bx = stacked ? x(acc) + (s > 0 ? gap : 0) : x(0);
                const bw = Math.max(v > 0 ? 2 : 0, (stacked ? x(acc + v) - x(acc) - (s > 0 ? gap : 0) : x(v) - x(0)));
                const by = stacked ? y0 : y0 + s * (bh + gap);
                acc += v;
                return (
                  <rect key={s} x={bx} y={by} width={bw} height={bh} rx={2} fill={series[s]}
                    {...on(<><b>{r.label}</b><br />{format(v)}{r.note ? <><br />{r.note}</> : null}</>)} />
                );
              })}
              {!stacked && r.values.map((v, s) => (
                <text key={`v${s}`} className="val" x={x(v) + 5} y={y0 + s * (bh + gap) + bh - 2}>{format(v)}</text>
              ))}
              {stacked && <text className="val" x={x(acc) + 5} y={y0 + bh - 2}>{r.note ?? ""}</text>}
              {m != null && <line x1={x(m)} x2={x(m)} y1={y0 - 3} y2={y0 + per + 3} stroke="var(--ink)" strokeWidth={2} />}
            </g>
          );
        })}
        <line className="ax" x1={x(0)} x2={x(0)} y1={0} y2={H - 18} />
      </svg>
      {node}
    </div>
  );
}

// ---------------------------------------------------------------------- lines

export interface Line { label: string; color: string; points: [number, number][] }

export function LineChart({ lines, xLabel, yFormat, xFormat = String, yMax, vline, height = 260 }: {
  lines: Line[]; xLabel: string; yFormat: (v: number) => string; xFormat?: (v: number) => string;
  yMax?: number; vline?: { x: number; label: string }; height?: number;
}) {
  const { on, node } = useTip();
  const { ref, w: W } = useWidth();
  const H = height, L = 52, R = 16, T = 10, B = 36;
  const xs = lines.flatMap((l) => l.points.map((p) => p[0]));
  const x0 = Math.min(...xs), x1 = Math.max(...xs);
  const yM = yMax ?? niceMax(Math.max(...lines.flatMap((l) => l.points.map((p) => p[1]))));
  const X = (v: number) => L + ((v - x0) / (x1 - x0 || 1)) * (W - L - R);
  const Y = (v: number) => T + (1 - v / yM) * (H - T - B);
  const yt = [0, 0.25, 0.5, 0.75, 1].map((t) => t * yM);
  const xt = [...new Set(xs)].sort((a, b) => a - b);
  const xticks = xt.length <= 8 && W > 420 ? xt : [0, 0.5, 1].map((t) => x0 + t * (x1 - x0));
  return (
    <div ref={ref}>
      <svg className="chart" viewBox={`0 0 ${W} ${H}`} role="img">
        {yt.map((t) => (
          <g key={t}>
            <line className="gl" x1={L} x2={W - R} y1={Y(t)} y2={Y(t)} />
            <text x={L - 8} y={Y(t) + 4} textAnchor="end">{yFormat(t)}</text>
          </g>
        ))}
        {xticks.map((t) => <text key={t} x={X(t)} y={H - B + 16} textAnchor="middle">{xFormat(t)}</text>)}
        <text x={(L + W - R) / 2} y={H - 4} textAnchor="middle">{xLabel}</text>
        <line className="ax" x1={L} x2={W - R} y1={Y(0)} y2={Y(0)} />
        {vline && (
          <g>
            <line x1={X(vline.x)} x2={X(vline.x)} y1={T} y2={Y(0)} stroke="var(--ink)" strokeWidth={1} strokeDasharray="3 3" />
            <text x={X(vline.x) + (X(vline.x) > W - 110 ? -6 : 6)} y={T + 12} textAnchor={X(vline.x) > W - 110 ? "end" : "start"} style={{ fill: "var(--ink)" }}>{vline.label}</text>
          </g>
        )}
        {lines.map((l) => (
          <g key={l.label}>
            <polyline points={l.points.map(([a, b]) => `${X(a)},${Y(b)}`).join(" ")} fill="none" stroke={l.color} strokeWidth={2} strokeLinejoin="round" />
            {l.points.length <= 12 && l.points.map(([a, b]) => (
              <circle key={a} cx={X(a)} cy={Y(b)} r={4} fill={l.color} stroke="var(--bg)" strokeWidth={2}
                {...on(<><b>{l.label}</b><br />{xLabel} {xFormat(a)}: {yFormat(b)}</>)} />
            ))}
          </g>
        ))}
        {/* hover targets for dense lines: one invisible column per x */}
        {lines.some((l) => l.points.length > 12) && xt.map((t, i) => (
          <rect key={t} x={X(t) - (W - L - R) / xt.length / 2} y={T} width={(W - L - R) / xt.length} height={H - T - B} fill="transparent"
            {...on(<><b>{xLabel} {xFormat(t)}</b>{lines.map((l) => <div key={l.label}>{l.label}: {yFormat(l.points[i]?.[1] ?? NaN)}</div>)}</>)} />
        ))}
      </svg>
      {node}
    </div>
  );
}

// ---------------------------------------------------------------------- dots with intervals

export interface DotRow { label: string; v: number; lo: number; hi: number; emphasis?: boolean }

export function DotCI({ rows, min, max, format, color = "var(--series-1)", labelW = 210, refLine }: {
  rows: DotRow[]; min: number; max: number; format: (v: number) => string; color?: string; labelW?: number;
  refLine?: { v: number; label: string };
}) {
  const { on, node } = useTip();
  const { ref, w: W } = useWidth();
  labelW = Math.min(labelW, Math.round(W * 0.4));
  const rh = rows.some((r) => wrap2(r.label, labelW - 12).length > 1) ? 32 : 24, H = rows.length * rh + 30;
  const X = (v: number) => labelW + ((v - min) / (max - min)) * (W - labelW - 20);
  const ticks = (W > 420 ? [0, 0.25, 0.5, 0.75, 1] : [0, 0.5, 1]).map((t) => min + t * (max - min));
  return (
    <div ref={ref}>
      <svg className="chart" viewBox={`0 0 ${W} ${H}`} role="img">
        {ticks.map((t) => (
          <g key={t}>
            <line className="gl" x1={X(t)} x2={X(t)} y1={0} y2={H - 20} />
            <text x={X(t)} y={H - 6} textAnchor="middle">{format(t)}</text>
          </g>
        ))}
        {refLine && (
          <g>
            <line x1={X(refLine.v)} x2={X(refLine.v)} y1={0} y2={H - 20} stroke="var(--ink-2)" strokeDasharray="3 3" />
            <text x={X(refLine.v) + 4} y={H - 22} style={{ fill: "var(--ink-2)", fontSize: 10 }}>{refLine.label}</text>
          </g>
        )}
        {rows.map((r, i) => {
          const y = i * rh + 12;
          return (
            <g key={r.label} {...on(<><b>{r.label}</b><br />{format(r.v)} (interval {format(r.lo)} – {format(r.hi)})</>)}>
              <rect x={0} y={y - rh / 2} width={W} height={rh} fill="transparent" />
              <text x={labelW - 10} y={y + 4} textAnchor="end" style={{ fill: "var(--ink)", fontWeight: r.emphasis ? 600 : 400 }}>
                {wrap2(r.label, labelW - 12).map((t, k, all) => (
                  <tspan key={k} x={labelW - 10} dy={k === 0 ? (all.length > 1 ? -6 : 0) : 12}>{t}</tspan>
                ))}
              </text>
              <line x1={X(r.lo)} x2={X(r.hi)} y1={y} y2={y} stroke={color} strokeWidth={2} strokeOpacity={0.45} />
              <circle cx={X(r.v)} cy={y} r={5} fill={color} stroke="var(--bg)" strokeWidth={2} />
            </g>
          );
        })}
      </svg>
      {node}
    </div>
  );
}

// ---------------------------------------------------------------------- data table

export function DataTable({ head, rows }: { head: string[]; rows: (string | number)[][] }) {
  return (
    <details>
      <summary style={{ cursor: "pointer", fontSize: "var(--fs-sm)", color: "var(--ink-2)" }}>Data table</summary>
      <div style={{ overflowX: "auto", marginTop: 8 }}>
        <table className="tbl">
          <thead><tr>{head.map((h, i) => <th key={h} className={i ? "num" : ""}>{h}</th>)}</tr></thead>
          <tbody>{rows.map((r, i) => <tr key={i}>{r.map((c, j) => <td key={j} className={j ? "num" : ""}>{c}</td>)}</tr>)}</tbody>
        </table>
      </div>
    </details>
  );
}
