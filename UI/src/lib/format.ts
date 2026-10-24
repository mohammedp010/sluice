export const ms = (v: number) => (v < 1 ? `${v.toFixed(2)} ms` : v < 100 ? `${v.toFixed(1)} ms` : v < 10_000 ? `${Math.round(v)} ms` : `${(v / 1000).toFixed(1)} s`);

export const nsToMs = (ns: number) => ns / 1e6;

export function usd(v: number | null | undefined): string {
  if (v === null || v === undefined) return "—";
  if (v === 0) return "$0";
  if (Math.abs(v) < 0.01) return `$${v.toFixed(6)}`;
  return `$${v.toFixed(2)}`;
}

export const pct = (v: number, digits = 0) => `${(v * 100).toFixed(digits)}%`;

export const int = (v: number | null | undefined) => (v === null || v === undefined ? "—" : v.toLocaleString("en-US"));

export function ago(iso: string | number): string {
  const t = typeof iso === "number" ? iso : Date.parse(iso);
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 5) return "just now";
  if (s < 60) return `${Math.round(s)}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return new Date(t).toISOString().slice(0, 10);
}

export const shortId = (id: string) => id.slice(0, 8);
