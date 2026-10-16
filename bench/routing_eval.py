"""Phase 6, step 2 of 2: which cheap check is worth its cost? (instant; re-run freely)

    python bench/routing_eval.py       # reads bench/data/routing_runs.jsonl (routing_collect.py)

Every policy answers with the cheap model, runs a check, and escalates to the strong model when
the check fires. Quality is checked against reference answers, never by an LLM. Cost uses the
token counts the models really produced, priced like a typical small/large pair (strong = 16x).
Random escalation lands on the straight line between always-cheap and always-strong, so a check
only adds value if its point is ABOVE that line.

Writes bench/results/routing-eval.{md,json,svg}.
"""
from __future__ import annotations

import json
import math
import random
import re
from pathlib import Path
from zlib import crc32

from sluice.quality.signals import heuristic_flags, parse_json

ROOT = Path(__file__).parent
OUT = ROOT / "results"
PRICES = {"cheap": (0.15, 0.60), "strong": (2.50, 10.00)}  # USD / Mtok in, out
FINAL = re.compile(r"final answer\s*(?:is)?\s*[:\-]?\s*(.+)", re.IGNORECASE)
NUM = re.compile(r"-?\d[\d,]*(?:\.\d+)?|-?\.\d+")
NUMBER_WORDS = {w: i for i, w in enumerate(
    ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve"])}
WORDS = re.compile(r"\b(" + "|".join(NUMBER_WORDS) + r")\b", re.IGNORECASE)  # "six sides"
_OV = ROOT / "data/routing_grade_overrides.json"
# manual audit results: {"<id>/<cheap|strong>": {"correct": bool, "why": "..."}}
OVERRIDES = {tuple(k.split("/")): v["correct"]
             for k, v in (json.loads(_OV.read_text()) if _OV.exists() else {}).items()}


# --- grading against the reference answer ------------------------------------------------------

def final_answer(text: str) -> str:
    """The model's stated answer: after the last 'Final answer', else the last non-empty line.
    Lenient on purpose: llama3.2:1b often writes 'the final answer is X' or skips the line,
    and this eval measures correctness, not obedience to the format."""
    m = FINAL.findall(text)
    if m:
        return m[-1].strip()
    lines = [ln for ln in text.strip().splitlines() if ln.strip()]
    return lines[-1].strip() if lines else ""


def as_number(s: str) -> float | None:
    """The answer's number: the last one after the last "=" ("23 * 47 = 1081"), else the last
    one ("the ball costs 5 cents"). Simple on purpose; the cases it gets wrong are fixed by
    hand in data/routing_grade_overrides.json, with a reason each."""
    s = WORDS.sub(lambda m: str(NUMBER_WORDS[m.group(0).lower()]), s.rsplit("=", 1)[-1])
    nums = NUM.findall(s)
    return float(nums[-1].replace(",", "")) if nums else None


def json_match(exp, got) -> bool:
    if isinstance(exp, dict):
        if not isinstance(got, dict):
            return False
        low = {str(k).lower(): v for k, v in got.items()}
        return all(k.lower() in low and json_match(v, low[k.lower()]) for k, v in exp.items())
    if isinstance(exp, list):
        if not isinstance(got, list) or len(got) != len(exp):
            return False
        left = list(got)
        for e in exp:
            hit = next((g for g in left if json_match(e, g)), None)
            if hit is None:
                return False
            left.remove(hit)
        return True
    if isinstance(exp, bool):
        return got is exp
    if isinstance(exp, (int, float)):
        g = as_number(got) if isinstance(got, str) else got
        return isinstance(g, (int, float)) and not isinstance(g, bool) and abs(g - exp) < 1e-6
    a, b = str(exp).lower().strip(), str(got).lower().strip()
    return a == b or a.rstrip("s") == b.rstrip("s")  # "apples" == "apple"


def correct(item: dict, text: str) -> bool:
    if item["kind"] == "json":
        return json_match(item["expected"], parse_json(text))
    ans = final_answer(text)
    if item["kind"] == "number":
        x = as_number(ans)
        return x is not None and abs(x - item["expected"]) < 1e-6 * max(1, abs(item["expected"]))
    return any(re.search(rf"\b{re.escape(a)}\b", ans.lower()) for a in item["expected"])


# --- signals: may look at the cheap answer, never at the reference ----------------------------

def canon(item: dict, text: str) -> str:
    """Normalised answer for self-consistency votes."""
    if item["kind"] == "json":
        v = parse_json(text)
        return json.dumps(v, sort_keys=True).lower() if v is not None else "<invalid>"
    ans = final_answer(text)
    if item["kind"] == "number":
        x = as_number(ans)
        return f"{x:g}" if x is not None else ans.lower()
    return re.sub(r"[^a-z0-9 ]", "", ans.lower()).strip()


def agrees(a: str, b: str) -> bool:
    return a == b or (bool(a) and bool(b) and (a in b or b in a))


def judge_says_no(text: str) -> bool | None:
    w = re.findall(r"[a-z]+", text.lower())
    return {"no": True, "yes": False}.get(w[0]) if w else None


def cost(role: str, r: dict) -> float:
    pin, pout = PRICES[role]
    return (r["prompt_tokens"] * pin + r["completion_tokens"] * pout) / 1e6


# --- policies ---------------------------------------------------------------------------------

def per_item(items, runs):
    rows = []
    for it in items:
        g = runs[(it["id"], "cheap", "greedy", 0)]
        s = runs[(it["id"], "strong", "greedy", 0)]
        samples = [runs[(it["id"], "cheap", "sample", n)] for n in range(3)]
        judge = runs[(it["id"], "cheap", "judge", 0)]
        c0 = canon(it, g["text"])
        votes = sum(agrees(c0, canon(it, x["text"])) for x in samples)
        flags = heuristic_flags(g["text"], g["finish_reason"], it["kind"] == "json")
        rows.append({
            "id": it["id"], "difficulty": it["difficulty"],
            "cheap_ok": OVERRIDES.get((it["id"], "cheap"), correct(it, g["text"])),
            "strong_ok": OVERRIDES.get((it["id"], "strong"), correct(it, s["text"])),
            "cost_cheap": cost("cheap", g), "cost_strong": cost("strong", s),
            "cost_samples": sum(cost("cheap", x) for x in samples),
            "cost_judge": cost("cheap", judge),
            "ms_cheap": g["ms"], "ms_strong": s["ms"],
            "ms_samples": sum(x["ms"] for x in samples), "ms_judge": judge["ms"],
            "flags": flags,
            "no_final_line": it["kind"] != "json" and not FINAL.search(g["text"]),
            "votes": votes, "judge_no": judge_says_no(judge["text"]),
            "judged": crc32(it["id"].encode()) % 2 == 0,  # the 50% sample, fixed per prompt
        })
    return rows


POLICIES = {  # name -> (escalate?, extra cost key, extra latency key, uses half sample)
    "heuristics": (lambda r: bool(r["flags"]), None, None),
    "heuristics + format": (lambda r: bool(r["flags"]) or r["no_final_line"], None, None),
    "self-consistency (any disagree)": (lambda r: r["votes"] < 3, "samples", "samples"),
    "self-consistency (majority)": (lambda r: r["votes"] < 2, "samples", "samples"),
    "1B judge": (lambda r: r["judge_no"] is True, "judge", "judge"),
    "1B judge on 50%": (lambda r: r["judged"] and r["judge_no"] is True, "judge50", "judge50"),
    "oracle (upper bound)": (lambda r: not r["cheap_ok"], None, None),
}


def run_policy(rows, name):
    if name == "always cheap":
        return [(r["cheap_ok"], r["cost_cheap"], r["ms_cheap"], False) for r in rows]
    if name == "always strong":
        return [(r["strong_ok"], r["cost_strong"], r["ms_strong"], True) for r in rows]
    fire, extra, _ = POLICIES[name]
    out = []
    for r in rows:
        esc = fire(r)
        c, ms = r["cost_cheap"], r["ms_cheap"]
        if extra == "samples":
            c, ms = c + r["cost_samples"], ms + r["ms_samples"]
        elif extra == "judge" or (extra == "judge50" and r["judged"]):
            c, ms = c + r["cost_judge"], ms + r["ms_judge"]
        if esc:
            c, ms = c + r["cost_strong"], ms + r["ms_strong"]
        out.append((r["strong_ok"] if esc else r["cheap_ok"], c, ms, esc))
    return out


def summarise(res, base_cost, rng, boots=2000):
    n = len(res)
    acc = sum(ok for ok, *_ in res) / n
    cost_ = sum(c for _, c, *_ in res) / n
    idx = range(n)
    accs, costs = [], []
    for _ in range(boots):
        s = [res[rng.choice(idx)] for _ in idx]
        accs.append(sum(ok for ok, *_ in s) / n)
        costs.append(sum(c for _, c, *_ in s) / n / base_cost)
    accs.sort()
    costs.sort()
    lo, hi = int(0.025 * boots), int(0.975 * boots) - 1
    return {"accuracy": acc, "acc_lo": accs[lo], "acc_hi": accs[hi],
            "rel_cost": cost_ / base_cost, "cost_lo": costs[lo], "cost_hi": costs[hi],
            "escalated": sum(e for *_, e in res) / n,
            "ms": sum(m for _, _, m, _ in res) / n}


# --- chart: cost vs accuracy, one point per policy, CI whiskers, random-escalation line ---------

def svg(points: dict) -> str:
    w, h, ml, mr, mt, mb = 720, 440, 56, 24, 40, 52
    xs = [p["rel_cost"] for p in points.values()] + [p["cost_hi"] for p in points.values()]
    xmax = max(1.1, max(xs) * 1.05)
    lo = min(p["acc_lo"] for p in points.values())
    ymin = max(0, math.floor(lo * 10 - 0.5)) / 10  # whole 10% steps, with room below

    def x(v):
        return ml + v / xmax * (w - ml - mr)

    def y(v):
        return mt + (1 - (v - ymin) / (1 - ymin)) * (h - mt - mb)

    o = [(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" '
          'font-family="system-ui, -apple-system, sans-serif" font-size="11" role="img" '
          'aria-label="Accuracy versus relative cost for each routing policy">'),
         "<style>",
         (":root{--bg:#fcfcfb;--ink:#0b0b0b;--ink2:#52514e;--grid:#e4e3df;--s1:#2a78d6}"
          "@media (prefers-color-scheme: dark){:root{--bg:#1a1a19;--ink:#f0efec;"
          "--ink2:#c3c2b7;--grid:#3a3936;--s1:#3987e5}}"),
         ".t{fill:var(--ink)} .m{fill:var(--ink2)} .g{stroke:var(--grid);stroke-width:1}",
         "</style>",
         f'<rect width="{w}" height="{h}" fill="var(--bg)"/>',
         (f'<text class="t" x="{ml}" y="18" font-weight="600">Accuracy vs cost per policy '
         f'(n={points["always cheap"]["n"]} prompts, 95% bootstrap CIs)</text>')]
    yt = [k / 10 for k in range(round(ymin * 10), 11)]
    for v in yt:
        o.append(f'<line class="g" x1="{ml}" x2="{w - mr}" y1="{y(v):.1f}" y2="{y(v):.1f}"/>'
                 f'<text class="m" x="{ml - 6}" y="{y(v) + 4:.1f}" text-anchor="end">'
                 f'{v:.0%}</text>')
    for v in [i * 0.25 for i in range(int(xmax / 0.25) + 1)]:
        o.append(f'<text class="m" x="{x(v):.1f}" y="{h - mb + 16}" text-anchor="middle">'
                 f'{v:g}×</text>')
    o.append(f'<text class="m" x="{(ml + w - mr) / 2}" y="{h - 14}" text-anchor="middle">'
             'cost relative to always-strong</text>')
    c, s = points["always cheap"], points["always strong"]
    o.append(f'<line x1="{x(c["rel_cost"]):.1f}" y1="{y(c["accuracy"]):.1f}" '
             f'x2="{x(s["rel_cost"]):.1f}" y2="{y(s["accuracy"]):.1f}" '
             'stroke-dasharray="4 4" style="stroke:var(--ink2)"/>')
    o.append(f'<text class="m" x="{x(0.6):.1f}" '
             f'y="{y((c["accuracy"] + s["accuracy"]) / 2) + 16:.1f}" text-anchor="middle">'
             'random escalation</text>')
    for name, p in points.items():
        px, py = x(p["rel_cost"]), y(p["accuracy"])
        base = name.startswith(("always", "oracle"))
        fill = "var(--bg)" if name.startswith("oracle") else (
            "var(--ink2)" if base else "var(--s1)")
        stroke = "var(--ink2)" if base else "var(--s1)"
        o.append(f'<line x1="{px:.1f}" x2="{px:.1f}" y1="{y(p["acc_lo"]):.1f}" '
                 f'y2="{y(p["acc_hi"]):.1f}" style="stroke:{stroke}" stroke-width="1.5" '
                 'opacity="0.5"/>')
        o.append(f'<line x1="{x(p["cost_lo"]):.1f}" x2="{x(p["cost_hi"]):.1f}" y1="{py:.1f}" '
                 f'y2="{py:.1f}" style="stroke:{stroke}" stroke-width="1.5" opacity="0.5"/>')
        o.append(f'<circle cx="{px:.1f}" cy="{py:.1f}" r="5" style="fill:{fill};'
                 f'stroke:{stroke}" stroke-width="2"><title>{name}: {p["accuracy"]:.0%} '
                 f'accurate at {p["rel_cost"]:.2f}× cost, {p["escalated"]:.0%} escalated'
                 '</title></circle>')
        dx, dy, anchor = p.get("label_at", (8, -8, "start"))
        o.append(f'<text class="t" x="{px + dx:.1f}" y="{py + dy:.1f}" '
                 f'text-anchor="{anchor}">{name}</text>')
    o.append("</svg>")
    return "\n".join(o) + "\n"


def main():
    items = [json.loads(x) for x in (ROOT / "data/routing_prompts.jsonl").read_text().splitlines()]
    runs = {}
    for line in (ROOT / "data/routing_runs.jsonl").read_text().splitlines():
        r = json.loads(line)
        runs[(r["id"], r["role"], r["mode"], r["n"])] = r
    rows = per_item(items, runs)
    rng = random.Random(0)
    base = sum(r["cost_strong"] for r in rows) / len(rows)
    names = ["always cheap", "always strong", *POLICIES]
    results = {}
    for nm in names:
        res = run_policy(rows, nm)
        results[nm] = {**summarise(res, base, rng), "n": len(rows)}

    wrong = [r for r in rows if not r["cheap_ok"]]
    table = []
    for nm, (fire, *_) in POLICIES.items():
        if nm.startswith("oracle"):
            continue
        fired = [r for r in rows if fire(r)]
        caught = [r for r in wrong if fire(r)]
        useful = [r for r in fired if not r["cheap_ok"] and r["strong_ok"]]
        table.append((nm, len(fired), sum(not r["cheap_ok"] for r in fired), len(caught),
                      len(useful)))

    # the judge as a classifier, and per-difficulty accuracy
    judged = [r for r in rows if r["judge_no"] is not None]
    unparsed = len(rows) - len(judged)
    judged_ok = [r for r in judged if r["cheap_ok"]]
    judged_bad = [r for r in judged if not r["cheap_ok"]]
    no_on_ok = sum(r["judge_no"] is True for r in judged_ok)
    no_on_bad = sum(r["judge_no"] is True for r in judged_bad)
    by_diff = {d: (sum(r["cheap_ok"] for r in rows if r["difficulty"] == d),
                   sum(r["strong_ok"] for r in rows if r["difficulty"] == d),
                   sum(r["difficulty"] == d for r in rows)) for d in ("easy", "hard")}
    flag_counts = {}
    for r in rows:
        for f in r["flags"]:
            flag_counts[f] = flag_counts.get(f, 0) + 1

    # label placement, checked by rendering the chart: points that coincide get labels on
    # opposite sides instead of on top of each other
    for nm, p in results.items():
        p["label_at"] = (8, -8, "start")
    results["heuristics"]["label_at"] = (8, 16, "start")
    results["heuristics + format"]["label_at"] = (0, 22, "middle")
    results["1B judge"]["label_at"] = (0, -12, "middle")
    results["always strong"]["label_at"] = (8, 16, "start")
    results["oracle (upper bound)"]["label_at"] = (8, 4, "start")

    OUT.mkdir(exist_ok=True)
    (OUT / "routing-eval.json").write_text(json.dumps(
        {"policies": results, "items": rows}, indent=1, default=str) + "\n")
    (OUT / "routing-eval.svg").write_text(svg(results))

    c, s = results["always cheap"], results["always strong"]

    def above_line(p):  # accuracy minus what random escalation buys at the same cost
        span = s["rel_cost"] - c["rel_cost"]
        frac = (p["rel_cost"] - c["rel_cost"]) / span if span else 0
        return p["accuracy"] - (c["accuracy"] + frac * (s["accuracy"] - c["accuracy"]))

    n = len(rows)
    md = [
        "# Quality-aware routing: does a cheap check beat paying for the strong model?",
        "",
        (f"Generated by `bench/routing_eval.py` from `bench/data/routing_runs.jsonl` "
         f"(collected by `bench/routing_collect.py`, local Ollama, $0). {n} prompts with "
         "reference answers, half easy and half hard. Cheap = `llama3.2:1b`, strong = "
         "`mistral` 7B. Grading compares against the reference; no LLM grades the results."),
        "",
        ("Cost is the real token counts at small/large-model list-style prices "
         f"(cheap ${PRICES['cheap'][0]}/${PRICES['cheap'][1]}, strong "
         f"${PRICES['strong'][0]}/${PRICES['strong'][1]} per Mtok in/out) and is shown "
         "relative to always-strong. Brackets are 95% bootstrap intervals over prompts: with "
         f"{n} prompts, differences under ~10 points are noise."),
        "",
        "![accuracy vs cost per routing policy](routing-eval.svg)",
        "",
        ("| policy | accuracy | cost vs always-strong | escalated | vs random escalation | "
        "mean latency |"),
        "|---|---|---|---|---|---|",
    ]
    for nm in names:
        p = results[nm]
        md.append(f"| {nm} | {p['accuracy']:.0%} [{p['acc_lo']:.0%}–{p['acc_hi']:.0%}] | "
                  f"{p['rel_cost']:.2f}× | {p['escalated']:.0%} | "
                  f"{above_line(p):+.0%} | {p['ms'] / 1000:.1f} s |")
    md += [
        "",
        "## Which check won",
        "",
        (f"The cheap model was wrong on {len(wrong)} of {n} prompts. A check *fires* when it "
         "escalates. It is *right to fire* when the cheap answer was wrong. It is *useful* "
         "when the strong model then got it right."),
        "",
        "| check | fired | right to fire | caught (of cheap-wrong) | useful escalations |",
        "|---|---|---|---|---|",
        *[f"| {nm} | {f} | {rf} ({rf / f:.0%}) | {cg}/{len(wrong)} ({cg / len(wrong):.0%}) "
          f"| {u} |" if f and wrong else f"| {nm} | {f} | {rf} | {cg}/{len(wrong)} | {u} |"
          for nm, f, rf, cg, u in table],
        "",
        f"Heuristic flags on cheap answers: {flag_counts or 'none'}.",
        "",
        "## Notes",
        "",
        (f"- By difficulty: cheap {by_diff['easy'][0]}/{by_diff['easy'][2]} easy, "
         f"{by_diff['hard'][0]}/{by_diff['hard'][2]} hard; strong {by_diff['easy'][1]}/"
         f"{by_diff['easy'][2]} easy, {by_diff['hard'][1]}/{by_diff['hard'][2]} hard."),
        (f"- The 1B judge said NO to {no_on_bad} of {len(judged_bad)} wrong answers but also "
         f"to {no_on_ok} of {len(judged_ok)} correct ones ({unparsed} verdicts unparseable): "
         "it barely discriminates, so escalating on its NO is close to escalating everything."),
        ("- Logprobs (the plan's confidence signal) aren't exposed by this Ollama version, so "
         "self-consistency stands in: 3 extra samples at temperature 0.7."),
        ("- Latency is sequential wall time on one M2 laptop, including model swaps. Read it "
         "as relative only."),
        ("- `heuristics + format` uses this eval's 'Final answer:' convention, which the "
         "gateway can't know in general, so it is not a production check."),
    ]
    (OUT / "routing-eval.md").write_text("\n".join(md) + "\n")
    print("\n".join(md))


if __name__ == "__main__":
    main()
