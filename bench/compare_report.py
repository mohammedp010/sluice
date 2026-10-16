"""Turn bench/compare.py runs into bench/results/compare.{md,svg}.

    python bench/compare_report.py before after     # tags of results/compare-<tag>.json

The last tag is the headline; an earlier one is shown as "before" for Sluice (the profiling fix).
Per (target, rate): the median across reps of each percentile, over *clean* reps. A rep where the
load generator itself ran >20 ms late (p99) measured the laptop, not the gateway, so it is left
out whenever a clean rep exists, and the count of left-out reps is shown. Success rates use
every rep.
"""
from __future__ import annotations

import json
import math
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).parent
OUT = ROOT / "results"
LAG_MS = 20
TARGETS = ("direct", "sluice", "litellm")
# categorical slots 1-2 (validated light/dark); "direct" is the baseline, so neutral grey
COLORS = {"direct": ("#8a8984", "#8a8984"), "sluice": ("#2a78d6", "#3987e5"),
          "litellm": ("#eb6834", "#d95926")}


def load(tag: str) -> dict:
    return json.loads((OUT / f"compare-{tag}.json").read_text())


def table(run: dict) -> dict:
    """(target, rps) -> latency medians over clean reps, worst success rate over all reps."""
    groups: dict = {}
    for r in run["sweep"]:
        groups.setdefault((r["target"], r["rps_target"]), []).append(r)
    out = {}
    for k, rs in groups.items():
        served = [r for r in rs if r["ok"]]
        clean = [r for r in served if r["gen_lag_p99_ms"] <= LAG_MS] or served
        out[k] = {m: (statistics.median(r[m] for r in clean) if clean else float("nan"))
                  for m in ("p50_ms", "p95_ms", "p99_ms")}
        out[k]["success"] = min(r["success_rate"] for r in rs)
        out[k]["reps"] = len(rs)
        out[k]["flagged"] = len(served) - len(clean)  # left out of the medians
    return out


def src_lines() -> int:
    return sum(len(p.read_text().splitlines()) for p in (ROOT.parent / "src").rglob("*.py"))


def svg(t: dict, rates: list[int]) -> str:
    pw, ph, ml, mr, gap, top = 330, 230, 52, 16, 30, 48
    w, h = 2 * pw + gap + 24, top + ph + 44
    ymax, ymin = {}, {}
    for m in ("p50_ms", "p99_ms"):
        vals = [t[(g, r)][m] for g in TARGETS for r in rates if (g, r) in t]
        ymax[m] = max(vals) * 1.1
        # lines, not bars: no zero baseline needed, and the gaps of interest are a few ms
        ymin[m] = max(0.0, (min(vals) // 10) * 10 - 10)

    def x(r):
        return ml + (r - rates[0]) / (rates[-1] - rates[0]) * (pw - ml - mr)

    o = [(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" '
          'font-family="system-ui, -apple-system, sans-serif" font-size="11" role="img" '
          'aria-label="Latency percentiles versus request rate for direct, Sluice and LiteLLM">'),
         "<style>",
         (":root{--bg:#fcfcfb;--ink:#0b0b0b;--ink2:#52514e;--grid:#e4e3df;"
          + "".join(f"--{g}:{c[0]};" for g, c in COLORS.items()) + "}"),
         ("@media (prefers-color-scheme: dark){:root{--bg:#1a1a19;--ink:#f0efec;--ink2:#c3c2b7;"
          "--grid:#3a3936;" + "".join(f"--{g}:{c[1]};" for g, c in COLORS.items()) + "}}"),
         ".t{fill:var(--ink)} .m{fill:var(--ink2)} .g{stroke:var(--grid);stroke-width:1}",
         ".l{fill:none;stroke-width:2;stroke-linejoin:round;stroke-linecap:round}",
         "</style>",
         f'<rect width="{w}" height="{h}" fill="var(--bg)"/>',
         '<g transform="translate(12,10)">']
    lx = 0
    for g in TARGETS:
        o.append(f'<line class="l" x1="{lx}" x2="{lx + 18}" y1="6" y2="6" '
                 f'style="stroke:var(--{g})"/><text class="t" x="{lx + 24}" y="10">{g}</text>')
        lx += 90
    o.append(f'<text class="m" x="{lx + 10}" y="10">median of reps · mock upstream adds 50 ms '
             '· ◇ = a stalled rep left out</text>')
    for col, m in enumerate(("p50_ms", "p99_ms")):
        ox = col * (pw + gap)

        def y(v, m=m):
            return top + (1 - (v - ymin[m]) / (ymax[m] - ymin[m])) * (ph - 26)

        o.append(f'<g transform="translate({ox},0)">')
        o.append(f'<text class="t" x="{ml}" y="{top - 14}" font-weight="600">'
                 f'{m[:3]} latency (ms)</text>')
        span = ymax[m] - ymin[m]
        step = 10 if span <= 80 else 50 if span <= 300 else (250 if span <= 1500 else 1000)
        v = math.ceil(ymin[m] / step) * step  # ticks on round numbers
        while v <= ymax[m]:
            o.append(f'<line class="g" x1="{ml}" x2="{pw - mr}" y1="{y(v):.1f}" y2="{y(v):.1f}"/>'
                     f'<text class="m" x="{ml - 6}" y="{y(v) + 4:.1f}" text-anchor="end">{v:g}'
                     '</text>')
            v += step
        for r in rates:
            o.append(f'<text class="m" x="{x(r):.1f}" y="{y(ymin[m]) + 16:.1f}" '
                     f'text-anchor="middle">{r}</text>')
        o.append(f'<text class="m" x="{(ml + pw - mr) / 2:.1f}" y="{y(ymin[m]) + 32:.1f}" '
                 'text-anchor="middle">requests / second (open loop)</text>')
        for g in TARGETS:
            pts = [(r, t[(g, r)]) for r in rates if (g, r) in t]
            d = " ".join(f"{'M' if i == 0 else 'L'}{x(r):.1f},{y(p[m]):.1f}"
                         for i, (r, p) in enumerate(pts))
            o.append(f'<path class="l" d="{d}" style="stroke:var(--{g})"/>')
            for r, p in pts:
                shape = (f'<path d="M{x(r):.1f},{y(p[m]) - 5:.1f} l5,5 l-5,5 l-5,-5 z" '
                         if p["flagged"] else f'<circle cx="{x(r):.1f}" cy="{y(p[m]):.1f}" r="3.5" ')
                o.append(f'{shape}style="fill:var(--{g});stroke:var(--bg)" stroke-width="1.5">'
                         f'<title>{g} @ {r} rps: {m[:3]} {p[m]:.1f} ms'
                         f'{" (generator lagged in " + str(p["flagged"]) + " rep)" if p["flagged"] else ""}'
                         f'</title></{"path" if p["flagged"] else "circle"}>')
        o.append("</g>")
    o.append("</g></svg>")
    return "\n".join(o) + "\n"


FEATURES = [  # (feature, Sluice, LiteLLM, edge)
    ("Providers", "1 OpenAI-compatible adapter + mock (Azure/Bedrock not built)",
     "100+ providers†", "LiteLLM"),
    ("Virtual keys", "hashed in SQLite, CLI only", "Postgres-backed, admin UI†", "LiteLLM"),
    ("RPM/TPM limits", "token buckets, Redis Lua; verified across 2 replicas here",
     "yes, Redis-backed†", "even (Sluice's tested here)"),
    ("Budgets", "per key, daily/monthly", "per key/team/user†", "LiteLLM"),
    ("Retries/fallback", "chain + jittered backoff + hand-written breaker",
     "fallbacks, retries, cooldowns", "even (both 100% in the failover test)"),
    ("Streaming failure", "fail over before first byte only; terminal error event (ADR 0001)",
     "not checked here", "-"),
    ("Tracing", "OTel GenAI spans per attempt, trace id ↔ cost ledger",
     "many integrations (Langfuse, OTel, Datadog...)†", "LiteLLM on breadth"),
    ("Semantic cache", "lexical guard; threshold from a labelled eval with false-hit rate",
     "Redis/Qdrant semantic cache†", "LiteLLM on maturity; Sluice on measured false hits"),
    ("Quality routing", "escalation on free checks; measured: no gain on this workload",
     "not checked here", "-"),
    ("Admin UI", "none", "yes†", "LiteLLM"),
    ("Size / maturity", "{lines} lines of Python, one learner, this repo",
     "large project, production users", "LiteLLM"),
]


def main(tags: list[str]):
    runs = [load(tg) for tg in tags]
    head = runs[-1]
    t = table(head)
    rates = sorted({k[1] for k in t})
    before = table(runs[0]) if len(runs) > 1 else None
    (OUT / "compare.svg").write_text(svg(t, rates))

    md = [
        "# Sluice vs LiteLLM: added latency and failure behaviour",
        "",
        (f"Generated by `bench/compare.py` (run tag `{tags[-1]}`) and `bench/compare_report.py`. "
         f"Machine: {head['env']['machine']}. Sluice on Python {head['env']['python_sluice']}, "
         f"LiteLLM {head['env']['litellm']}. Each gateway is one uvicorn worker on uvloop with "
         "access logs off, in front of the same raw-ASGI mock upstream (fixed 50 ms). The load "
         "is open loop (`bench/loadgen.py`), measured from each request's scheduled send time, "
         "so queueing is not hidden (coordinated omission)."),
        "",
        ("Everything shares one 8-core laptop: generator, mocks and gateway. Absolute numbers "
         "are specific to it; the comparison between gateways is the point. Above ~300 rps the "
         "generator itself falls behind, so no claims are made there."),
        "",
        "![latency percentiles vs request rate](compare.svg)",
        "",
        "## Added latency over direct (median of reps, ms)",
        "",
        "| rps | direct p50 / p99 | Sluice +p50 / +p99 | LiteLLM +p50 / +p99 | reps left out |",
        "|---|---|---|---|---|",
    ]
    for r in rates:
        d = t.get(("direct", r))
        if not d:
            continue
        cells = []
        flags = []
        for g in ("sluice", "litellm"):
            p = t.get((g, r))
            if p is None:
                cells.append("saturated")
                continue
            ok = "" if p["success"] == 1 else f" ({p['success']:.1%} ok)"
            cells.append(f"{p['p50_ms'] - d['p50_ms']:+.1f} / {p['p99_ms'] - d['p99_ms']:+.1f}{ok}")
            if p["flagged"]:
                flags.append(f"{g} {p['flagged']}/{p['reps']}")
        if d["flagged"]:
            flags.append(f"direct {d['flagged']}/{d['reps']}")
        md.append(f"| {r} | {d['p50_ms']:.1f} / {d['p99_ms']:.1f} | {cells[0]} | {cells[1]} | "
                  f"{', '.join(flags) or '-'} |")
    md += [
        "",
        ("Percentiles don't subtract exactly (the p99 request through a gateway isn't the p99 "
         "request direct), so read the differences as approximate. Both gateways add the most "
         "at the lowest rate; a likely (unverified) cause is CPU frequency scaling on a mostly "
         "idle laptop. Reps left out had the load "
         f"generator itself more than {LAG_MS} ms behind schedule (a laptop stall); their raw "
         "numbers are in the JSON."),
    ]
    if before:
        md += ["", (f"## Sluice before / after the Phase 7 fixes (ADR 0007; `{tags[0]}` → "
               f"`{tags[-1]}`)"), "",
               ("Rates run in increasing order inside each rep, so rep 2 and 3 of `before` "
                "started right after the 250 rps overload of the rep before. That's where the "
                "pool leak left Sluice answering nothing."), "",
               ("| rps | worst-rep success before → after | p50 before → after | "
               "p99 before → after |"), "|---|---|---|---|"]

        def f(v):
            return "-" if math.isnan(v) else f"{v:.1f}"  # nan: no request succeeded

        for r in rates:
            b, a = before.get(("sluice", r)), t.get(("sluice", r))
            if b and a:
                md.append(f"| {r} | {b['success']:.0%} → {a['success']:.0%} | "
                          f"{f(b['p50_ms'])} → {f(a['p50_ms'])} | "
                          f"{f(b['p99_ms'])} → {f(a['p99_ms'])} |")
    md += ["", "## Failover: primary fails 30%, healthy backup, 50 rps (one 20 s run each)", "",
           "| target | success | p50 | p99 | statuses |", "|---|---|---|---|---|"]
    for f in head["failover"]:
        md.append(f"| {f['target']} | {f['success_rate']:.1%} | {f['p50_ms']:.1f} | "
                  f"{f['p99_ms']:.1f} | {f['statuses']} |")
    md += [
        "",
        ("Both gateways: retry the primary once, then fall back. Sluice also has a per-provider "
         "breaker (3 consecutive failures, 10 s cooldown); LiteLLM has its own cooldowns. Configs "
         "are written out in full in `bench/compare.py`."),
        "",
        "## Feature comparison (including where Sluice loses)",
        "",
        "| | Sluice | LiteLLM | edge |",
        "|---|---|---|---|",
        *[f"| {a} | {b.format(lines=f'{src_lines():,}')} | {c} | {d} |"
          for a, b, c, d in FEATURES],
        "",
        ("† from LiteLLM's documentation, not exercised in this benchmark. Only latency and "
         "the failover scenario above were measured head to head."),
    ]
    (OUT / "compare.md").write_text("\n".join(md) + "\n")
    print("\n".join(md))


if __name__ == "__main__":
    main(sys.argv[1:] or ["run"])
