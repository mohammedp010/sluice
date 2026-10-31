export type PanelId = "lab" | "topology" | "ledger" | "evidence" | "decisions" | "build" | "mcp" | "keys" | "security";

export function go(panel: PanelId, arg = "") {
  location.hash = `#${panel}${arg ? `/${encodeURIComponent(arg)}` : ""}`;
}
