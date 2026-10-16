"""Phase 5 "done when": pick the cache threshold from a curve, report false hits on held-out data.

    python bench/cache_eval.py            # needs Ollama with nomic-embed-text and all-minilm

For each embedding model: embed both sides of every labelled pair (bench/data/cache_pairs.jsonl)
and sweep the similarity threshold, with and without the lexical guard (sluice.cache.semantic).
A pair is a cache *hit* when cos(a, b) >= t and the guard doesn't veto it.
  precision  = correct hits / all hits     (1 - precision = false-hit rate: wrong answers served)
  recall     = correct hits / true repeats (the hit rate you get on genuine repeats)
Selection touches the dev split only: for each (model, guard) the LOWEST threshold with zero
false hits on dev, then the combination with the best dev recall. A miss costs one upstream call;
a false hit serves the answer to a different question. The test split is read once, to report.

Writes bench/results/cache-eval.{md,json,svg}.
"""
from __future__ import annotations

import asyncio
import json
import statistics
import time
from pathlib import Path

import numpy as np

from sluice.cache.embed import OpenAICompatEmbedder
from sluice.cache.semantic import token_diff

ROOT = Path(__file__).parent
OUT = ROOT / "results"
BASE = "http://localhost:11434/v1"
VARIANTS = [  # (label, model, prefix)
    ("all-minilm", "all-minilm", ""),
    ("nomic-embed-text", "nomic-embed-text", ""),
    ("nomic + 'clustering: '", "nomic-embed-text", "clustering: "),
]
GUARDS = [None, 2, 3, 4]  # veto hits whose word sets differ by <= k words (None = no guard)
THRESHOLDS = [round(0.5 + i * 0.005, 3) for i in range(101)]  # 0.500 .. 1.000


def counts(hit: np.ndarray, same: np.ndarray) -> dict:
    tp, fp, fn = int((hit & same).sum()), int((hit & ~same).sum()), int((~hit & same).sum())
    return {"tp": tp, "fp": fp, "fn": fn,
            "precision": tp / (tp + fp) if tp + fp else None,
            "recall": tp / (tp + fn) if tp + fn else None}


async def embed_pairs(model: str, prefix: str, pairs: list[dict]) -> tuple[np.ndarray, dict]:
    emb = OpenAICompatEmbedder(BASE, model, prefix, timeout_s=60)
    texts = sorted({p["a"] for p in pairs} | {p["b"] for p in pairs})
    vecs = await emb.embed(texts)
    ix = {t: i for i, t in enumerate(texts)}
    sims = np.array([float(vecs[ix[p["a"]]] @ vecs[ix[p["b"]]]) for p in pairs])
    # latency of what the gateway does on every cached-route request: embed one short text
    await emb.embed(["warm up"])
    lat = []
    for p in pairs[:30]:
        t0 = time.perf_counter()
        await emb.embed([p["a"]])
        lat.append((time.perf_counter() - t0) * 1000)
    await emb.aclose()
    return sims, {"dim": int(vecs.shape[1]), "p50": statistics.median(lat),
                  "p95": sorted(lat)[int(0.95 * len(lat)) - 1]}


def evaluate(label, sims, diffs, same, dev, guard) -> dict:
    allowed = np.ones_like(same) if guard is None else ~((diffs >= 0) & (diffs <= guard))
    curve = [{"t": t, **counts((sims >= t) & allowed & dev, same & dev)} for t in THRESHOLDS]
    chosen = next(c["t"] for c in curve if c["fp"] == 0)
    hit = (sims >= chosen) & allowed
    return {"label": label, "guard": guard, "threshold": chosen, "curve_dev": curve,
            "dev": counts(hit & dev, same & dev), "test": counts(hit & ~dev, same & ~dev),
            "hit": hit}


def pair_list(pairs, sims, mask, key=None, n=None):
    rows = [(p, float(s)) for p, s, m in zip(pairs, sims, mask) if m]
    rows.sort(key=key or (lambda r: -r[1]))
    return [{"a": p["a"], "b": p["b"], "sim": round(s, 4)} for p, s in rows[:n]]


# --- chart: 2x2 small multiples, model (rows) x guard off/on (columns), dev split ------------

COLORS = {"light": ("#2a78d6", "#eb6834"), "dark": ("#3987e5", "#d95926")}


def svg(panels: list[list[dict]]) -> str:
    pw, ph, ml, mr, gap = 330, 200, 44, 18, 28
    top = 34
    pad = 12  # outer margin so nothing touches the image edge
    w = 2 * pw + gap + 2 * pad
    h = top + len(panels) * (ph + gap) + pad

    def x(t):
        return ml + (t - 0.5) / 0.5 * (pw - ml - mr)

    def y(v):
        return 26 + (1 - v) * (ph - 26 - 40)

    out = [
        (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" '
         f'font-family="system-ui, -apple-system, sans-serif" font-size="11" role="img" '
         f'aria-label="Precision and recall versus similarity threshold, with and without '
         f'the lexical guard">'),
        "<style>",
        (f":root{{--bg:#fcfcfb;--ink:#0b0b0b;--ink2:#52514e;--grid:#e4e3df;"
         f"--s1:{COLORS['light'][0]};--s2:{COLORS['light'][1]}}}"),
        (f"@media (prefers-color-scheme: dark){{:root{{--bg:#1a1a19;--ink:#f0efec;"
         f"--ink2:#c3c2b7;--grid:#3a3936;--s1:{COLORS['dark'][0]};--s2:{COLORS['dark'][1]}}}}}"),
        ".t{fill:var(--ink)} .m{fill:var(--ink2)} .g{stroke:var(--grid);stroke-width:1}",
        (".p{stroke:var(--s1)} .r{stroke:var(--s2)} "
         ".l{fill:none;stroke-width:2;stroke-linejoin:round;stroke-linecap:round}"),
        "</style>",
        f'<rect width="{w}" height="{h}" fill="var(--bg)"/>',
        f'<g transform="translate({pad},{pad})">',
        '<line class="l p" x1="0" y1="12" x2="18" y2="12"/>',
        '<text class="t" x="24" y="16">Precision (1 − false-hit rate)</text>',
        '<line class="l r" x1="200" y1="12" x2="218" y2="12"/>',
        '<text class="t" x="224" y="16">Recall (hit rate on true repeats)</text>',
        '<text class="m" x="430" y="16">dev split · dashed = chosen threshold</text>',
    ]
    for row, pair in enumerate(panels):
        for col, res in enumerate(pair):
            ox, oy = col * (pw + gap), top + row * (ph + gap)
            guard = "no guard" if res["guard"] is None else f"guard ≤{res['guard']} words"
            out.append(f'<g transform="translate({ox},{oy})">')
            out.append(f'<text class="t" x="{ml}" y="12" font-weight="600">'
                       f'{res["label"]} · {guard}</text>')
            for v in (0, 0.5, 1):
                out.append(f'<line class="g" x1="{ml}" x2="{pw - mr}" y1="{y(v)}" y2="{y(v)}"/>')
                out.append(f'<text class="m" x="{ml - 6}" y="{y(v) + 4}" '
                           f'text-anchor="end">{v:g}</text>')
            for t in (0.5, 0.75, 1.0):
                out.append(f'<text class="m" x="{x(t)}" y="{y(0) + 16}" '
                           f'text-anchor="middle">{t:g}</text>')
            out.append(f'<text class="m" x="{(ml + pw - mr) / 2}" y="{y(0) + 32}" '
                       f'text-anchor="middle">similarity threshold</text>')
            for key, cls in (("precision", "p"), ("recall", "r")):
                pts = [(x(c["t"]), y(c[key])) for c in res["curve_dev"] if c[key] is not None]
                d = " ".join(f"{'M' if i == 0 else 'L'}{px:.1f},{py:.1f}"
                             for i, (px, py) in enumerate(pts))
                out.append(f'<path class="l {cls}" d="{d}"><title>{key}</title></path>')
            ct, rec = res["threshold"], res["dev"]["recall"]
            out.append(f'<line x1="{x(ct)}" x2="{x(ct)}" y1="{y(1) - 4}" y2="{y(0)}" '
                       f'stroke-dasharray="3 3" style="stroke:var(--ink2)"/>')
            anchor, dx = ("end", -5) if ct > 0.8 else ("start", 5)
            out.append(f'<text class="t" x="{x(ct) + dx}" y="{y(0.08)}" '
                       f'text-anchor="{anchor}">t = {ct:.3f} · recall {rec:.0%}</text>')
            out.append("</g>")
    out.append("</g></svg>")
    return "\n".join(out) + "\n"


def pct(n, d):
    return f"{n / d:.0%}" if d else "n/a"


def report(results, best, lat, pairs, sims_best, n) -> str:
    t = best["test"]
    hits = t["tp"] + t["fp"]
    guard = "no guard" if best["guard"] is None else f"guard ≤{best['guard']} words"
    if t["fp"] == 0 and hits:
        verdict = (f"With {hits} test hits and 0 false, the \"rule of three\" puts the 95% "
                   f"upper bound on the false-hit rate near 3/{hits} = {3 / hits:.0%}.")
    else:
        verdict = (f"Zero false hits on dev became **{t['fp']} of {hits} ({pct(t['fp'], hits)})"
                   f" on test**: {n['dev']} dev pairs were too few to expose them. Treat this "
                   "threshold as provisional until the eval set is bigger.")
    same = np.array([p["same"] for p in pairs])
    dev = np.array([p["split"] == "dev" for p in pairs])
    cat = np.array([p["category"] for p in pairs])
    lines = [
        "# Semantic cache: threshold choice and false-hit rate",
        "",
        ("Generated by `bench/cache_eval.py` (local Ollama embeddings, $0) from "
         f"`bench/data/cache_pairs.jsonl`: {n['total']} labelled pairs, {n['dev']} dev / "
         f"{n['test']} test."),
        "",
        ("> **Caveats.** The labels are a first draft; see `bench/data/README.md`. The hard "
         "negatives were written as minimal pairs and the paraphrases as rewrites, which "
         "flatters the lexical guard. Every number here moves when the data does."),
        "",
        ("Rule (dev only): per (model, guard), the lowest threshold with **zero dev false "
         "hits**; then the combination with the best dev recall. Test is read once."),
        "",
        "![precision and recall vs threshold, with and without the lexical guard](cache-eval.svg)",
        "",
        "## Result",
        "",
        (f"**Chosen: `{best['label']}`, {guard}, threshold {best['threshold']:.3f}.** "
         f"On test: hit rate on true repeats **{pct(t['tp'], t['tp'] + t['fn'])}** "
         f"({t['tp']}/{t['tp'] + t['fn']}), false hits **{t['fp']} of {hits} served "
         f"({pct(t['fp'], hits)})**. {verdict}"),
        "",
        "| model | guard | threshold | dev recall | test recall | test false hits |",
        "|---|---|---|---|---|---|",
    ]
    for r in results:
        tt, dv = r["test"], r["dev"]
        th = tt["tp"] + tt["fp"]
        g = "none" if r["guard"] is None else f"≤{r['guard']}"
        mark = " **← chosen**" if r is best else ""
        lines.append(f"| {r['label']} | {g} | {r['threshold']:.3f} | "
                     f"{pct(dv['tp'], dv['tp'] + dv['fn'])} | "
                     f"{pct(tt['tp'], tt['tp'] + tt['fn'])} | {tt['fp']}/{th}{mark} |")
    lines += [
        "",
        ("Without the guard no model gets a usable hit rate at zero false hits: "
         "direction swaps (\"5 km to miles\" / \"5 miles to km\") score higher than most "
         "real paraphrases. Embeddings encode topic, and those pairs share it exactly."),
        "",
        "## Embedding cost (paid on every request to a cached route, hit or miss)",
        "",
        "| model | dim | p50 | p95 |",
        "|---|---|---|---|",
        *[f"| {lbl} | {v['dim']} | {v['p50']:.0f} ms | {v['p95']:.0f} ms |"
          for lbl, v in lat.items()],
        "",
        f"## Where `{best['label']}` goes wrong",
        "",
        "Most similar hard negatives (embedding alone would serve the wrong cached answer):",
        "",
        *[f"- {h['sim']:.3f}: \"{h['a']}\" / \"{h['b']}\""
          for h in pair_list(pairs, sims_best, cat == "hard_negative", n=5)],
        "",
        "Test false hits at the chosen setting:",
        "",
        *([f"- {h['sim']:.3f}: \"{h['a']}\" / \"{h['b']}\""
           for h in pair_list(pairs, sims_best, best["hit"] & ~same & ~dev)] or ["- none"]),
        "",
        "Hardest test misses (real repeats the cache won't catch):",
        "",
        *[f"- {h['sim']:.3f}: \"{h['a']}\" / \"{h['b']}\""
          for h in pair_list(pairs, sims_best, ~best["hit"] & same & ~dev,
                             key=lambda r: r[1], n=5)],
        "",
        "Raw curves: `cache-eval.json`.",
    ]
    return "\n".join(lines) + "\n"


async def main():
    pairs = [json.loads(line) for line in (ROOT / "data/cache_pairs.jsonl").read_text().splitlines()]
    same = np.array([p["same"] for p in pairs])
    dev = np.array([p["split"] == "dev" for p in pairs])
    diffs = np.array([token_diff(p["a"], p["b"]) for p in pairs])
    results, sims_by, lat = [], {}, {}
    for label, model, prefix in VARIANTS:
        sims, lat[label] = await embed_pairs(model, prefix, pairs)
        sims_by[label] = sims
        results += [evaluate(label, sims, diffs, same, dev, g) for g in GUARDS]
    # best dev recall; ties go to the stricter guard, then the higher threshold
    best = max(results, key=lambda r: (r["dev"]["tp"], r["guard"] or 0, r["threshold"]))
    n = {"total": len(pairs), "dev": int(dev.sum()), "test": int((~dev).sum())}

    by = {(r["label"], r["guard"]): r for r in results}
    chart_models = [best["label"]] + [v[0] for v in VARIANTS[:2] if v[0] != best["label"]][:1]
    panels = [[by[(m, None)], by[(m, best["guard"])]] for m in chart_models]

    OUT.mkdir(exist_ok=True)
    (OUT / "cache-eval.json").write_text(json.dumps(
        [{k: v for k, v in r.items() if k != "hit"} for r in results], indent=1) + "\n")
    (OUT / "cache-eval.svg").write_text(svg(panels))
    md = report(results, best, lat, pairs, sims_by[best["label"]], n)
    (OUT / "cache-eval.md").write_text(md)
    print(md)


if __name__ == "__main__":
    asyncio.run(main())
