import { Check, CircleDashed, Minus, OctagonX, RotateCw, X, Zap, type LucideIcon } from "lucide-react";
import type { BreakerState } from "../lib/api";
import type { Verdict } from "../lib/trace";

// State is a mark, not a hue: every status has its own glyph and words, so it reads without
// colour (and in forced-colours mode).

const VERDICT: Record<Verdict, { icon: LucideIcon; word: string }> = {
  pass: { icon: Check, word: "passed" },
  fail: { icon: X, word: "failed" },
  warn: { icon: RotateCw, word: "recovered" },
  hit: { icon: Zap, word: "cache hit" },
  skip: { icon: Minus, word: "skipped" },
  idle: { icon: Minus, word: "not reached" },
};

export function VerdictIcon({ v, size = 14 }: { v: Verdict; size?: number }) {
  const { icon: I, word } = VERDICT[v];
  return <I className={`v-${v}`} width={size} height={size} aria-label={word} strokeWidth={2.4} />;
}

export function VerdictMark({ v, text }: { v: Verdict; text?: string }) {
  return (
    <span className={`mark v-${v}`}>
      <VerdictIcon v={v} />
      <span className="t">{text ?? VERDICT[v].word}</span>
    </span>
  );
}

const BREAKER: Record<BreakerState, { icon: LucideIcon; cls: string; word: string; help: string }> = {
  closed: { icon: Check, cls: "v-pass", word: "closed", help: "Breaker closed: traffic flows" },
  half_open: { icon: CircleDashed, cls: "v-warn", word: "half-open", help: "Breaker half-open: one probe allowed" },
  open: { icon: OctagonX, cls: "v-fail", word: "open", help: "Breaker open: provider is skipped until cooldown" },
};

export function BreakerMark({ state, withWord = true }: { state: BreakerState; withWord?: boolean }) {
  const b = BREAKER[state];
  return (
    <span className={`mark ${b.cls}`} title={b.help}>
      <b.icon width={14} height={14} strokeWidth={2.4} aria-hidden />
      {withWord && <span className="t">{b.word}</span>}
    </span>
  );
}

export const breakerHelp = (s: BreakerState) => BREAKER[s].help;

/** HTTP status / outcome as a pill. */
export function StatusPill({ status, outcome }: { status: number; outcome?: string }) {
  const resumed = outcome === "resumed";
  const cls = status >= 500 || outcome === "stream_interrupted" ? "bad" : status >= 400 ? "warn" : resumed ? "warn" : "ok";
  return (
    <span className={`pill ${cls}`}>
      {status || "—"}
      {outcome && outcome !== "ok" ? ` ${outcome.replaceAll("_", " ")}` : ""}
    </span>
  );
}
