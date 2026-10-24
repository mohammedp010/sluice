import type { Overview, Provider, Route } from "./api";

/** What a provider will do to a request, read off its policy knobs. */
export function providerBehaviour(p: Provider | undefined): string {
  if (!p) return "unknown provider";
  if (p.type === "openai_compat") return p.base_url?.includes("11434") ? "local Ollama" : (p.base_url ?? "remote");
  const out: string[] = [];
  if (p.error_rate >= 1) out.push(`always fails (${p.error_status})`);
  else if (p.error_rate > 0) out.push(`fails ${Math.round(p.error_rate * 100)}% (${p.error_status})`);
  if (p.cut_stream_after !== null) out.push(`cuts streams after ${p.cut_stream_after} tokens`);
  if (p.text === "") out.push("answers empty");
  if (p.latency_ms) out.push(`+${p.latency_ms} ms`);
  return out.length ? `mock: ${out.join(", ")}` : "mock: healthy";
}

/** One line on what sending to this route will show. */
export function describeRoute(r: Route, o: Overview): string {
  const parts = [r.chain.map((t) => t.provider).join(" → ")];
  if (r.escalate) parts.push(`escalates to ${r.escalate.chain.map((t) => t.provider).join(" → ")} on ${r.escalate_on.join(", ")}`);
  if (r.stream_resume !== "off") parts.push(`stream resume: ${r.stream_resume}`);
  if (r.cache && o.cache.enabled) parts.push("semantic cache on");
  return parts.join(" · ");
}

/** Routes whose first provider cuts streams only show their story when streamed. */
export function wantsStream(r: Route, o: Overview): boolean {
  return o.providers[r.chain[0].provider]?.cut_stream_after != null;
}
