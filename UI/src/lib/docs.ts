// Repo files the UI reads at build time: ADRs, learning notes, and every benchmark result.
// Vite inlines them, so the Evidence / Decisions / Build log panels work with the gateway off.

const raw = (g: Record<string, unknown>) =>
  Object.fromEntries(Object.entries(g).map(([p, v]) => [p.split("/").pop()!, v as string]));

export const adrs = raw(import.meta.glob("../../../docs/decisions/*.md", { query: "?raw", import: "default", eager: true }));
export const notes = raw(import.meta.glob("../../../docs/learning/*.md", { query: "?raw", import: "default", eager: true }));
export const results = raw(import.meta.glob("../../../bench/results/*.{md,txt}", { query: "?raw", import: "default", eager: true }));
export const resultJson = Object.fromEntries(
  // named, not *.json: some result files hold NaN (valid for Python's json, not for JSON.parse)
  Object.entries(import.meta.glob([
    "../../../bench/results/chaos.json", "../../../bench/results/ratelimit.json",
    "../../../bench/results/cache-eval.json", "../../../bench/results/routing-eval-*.json",
    "../../../bench/results/compare-after.json",
  ], { import: "default", eager: true }))
    .map(([p, v]) => [p.split("/").pop()!, v]),
) as Record<string, unknown>;
export const resultSvgs = raw(import.meta.glob("../../../bench/results/*.svg", { query: "?url", import: "default", eager: true }));

export interface Adr { n: string; file: string; title: string; status: string; body: string }

export const adrList: Adr[] = Object.entries(adrs)
  .sort(([a], [b]) => a.localeCompare(b))
  .map(([file, body]) => {
    const n = file.slice(0, 4);
    const title = (/^#\s+(.*)$/m.exec(body)?.[1] ?? file).replace(/^ADR \d+:\s*/, "");
    const status = /\*\*Status:\*\*\s*([^\n]*(?:\n(?!\n)[^\n]*)*)/.exec(body)?.[1].replace(/\s+/g, " ").trim() ?? "";
    return { n, file, title, status, body };
  });

/** "Phase 3" -> the ADR numbers whose status line says they were accepted in that phase. */
export const adrsForPhase = (phase: number) => adrList.filter((a) => new RegExp(`Phase ${phase}\\b`).test(a.status));
