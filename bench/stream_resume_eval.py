"""Phase 8: does resuming a dead stream on another provider produce a clean answer?

    python bench/stream_resume_eval.py        # ~10 min local Ollama; resumable; $0

Every prompt asks for a numbered list of known length, so a bad seam is countable, not a matter
of taste: a repeated item (duplication), a missing one (gap), or "1, 2, 3 ... 1, 2, 3" (the
backup started over). For each prompt, qwen2.5:3b writes the full answer; it is cut at 30% and
65% (at a word boundary, mid-line allowed: real cuts land between tokens); then a backup model
continues from the cut using the gateway's own code (sluice.router.resume):
  error     what ADR 0001 does today: the client keeps the partial answer plus an error event
  restart   a silent restart: the backup answers from scratch, appended after the prefix
  prefill   prefix sent as a trailing assistant message          (+ seam dedupe, or raw)
  instruct  prefix sent, then "continue exactly where you stopped" (+ seam dedupe, or raw)
Backups: qwen2.5:3b (same model resumes) and llama3.2:1b (a different model takes over).
Writes bench/results/stream-resume.md; raw calls in bench/data/resume_runs.jsonl.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import re
from pathlib import Path

from sluice.providers.base import ChatParams, Message
from sluice.providers.openai_compat import OpenAICompatProvider
from sluice.router.resume import continuation_params, overlap

ROOT = Path(__file__).parent
RUNS = ROOT / "data/resume_runs.jsonl"
OUT = ROOT / "results"
WRITER = "qwen2.5:3b"
BACKUPS = ["qwen2.5:3b", "llama3.2:1b"]
CUTS = [0.30, 0.65]
FMT = " One item per line, written as '<number>. <item>', and nothing else."
PROMPTS = [  # (id, prompt, number of items)
    ("count40", "List the numbers from 1 to 40 as a numbered list." + FMT, 40),
    ("animals", "Name 15 different animals." + FMT, 15),
    ("months", "List the 12 months of the year in order." + FMT, 12),
    ("days", "List the 7 days of the week, starting with Monday." + FMT, 7),
    ("bwords", "Give 20 different English words that start with the letter B." + FMT, 20),
    ("europe", "Name 10 countries in Europe." + FMT, 10),
    ("primes", "List the first 15 prime numbers." + FMT, 15),
    ("sleep", "Give 12 tips for better sleep, one short sentence each." + FMT, 12),
    ("alphabet", "List the 26 letters of the English alphabet in order." + FMT, 26),
    ("langs", "Name 15 programming languages." + FMT, 15),
    ("colors", "Name 10 colors." + FMT, 10),
    ("planets", "List the planets of the solar system in order from the Sun." + FMT, 8),
    ("fruits", "Name 15 fruits." + FMT, 15),
    ("instruments", "Name 12 musical instruments." + FMT, 12),
    ("sevens", "List the multiples of 7 from 7 to 105." + FMT, 15),
    ("tea", "Write the steps to make a cup of tea, one short sentence each, in 10 steps." + FMT,
     10),
]
ITEM = re.compile(r"^\s*(\d+)[.)]\s+\S", re.MULTILINE)


def numbering(text: str) -> list[int]:
    return [int(n) for n in ITEM.findall(text)]


def grade(text: str, n: int) -> dict:
    nums = numbering(text)
    dup = len(nums) != len(set(nums))
    return {"valid": nums == list(range(1, n + 1)), "dup": dup,
            "gap": not dup and bool(nums) and nums != list(range(1, max(nums) + 1)),
            "restart": any(b < a for a, b in itertools.pairwise(nums))}


def cut_at(text: str, frac: float) -> str:
    """Cut just after a space or newline near `frac` of the way through."""
    i = int(len(text) * frac)
    while i < len(text) and text[i - 1] not in " \n":
        i += 1
    return text[:i]


async def main():
    done = {}
    if RUNS.exists():
        for line in RUNS.read_text().splitlines():
            r = json.loads(line)
            done[tuple(r["key"])] = r["text"]
    out = RUNS.open("a")
    p = OpenAICompatProvider("ollama", "http://localhost:11434/v1", timeout_s=300)

    async def call(key: tuple, model: str, msgs: list[Message], max_tokens: int = 600) -> str:
        if key not in done:
            c = await p.complete(ChatParams(model, msgs, 0.0, max_tokens))
            done[key] = c.content
            out.write(json.dumps({"key": list(key), "text": c.content}) + "\n")
            out.flush()
            print(key, repr(c.content[:50]), flush=True)
        return done[key]

    refs = {}
    for pid, prompt, _ in PROMPTS:
        refs[pid] = await call(("ref", pid), WRITER, [Message("user", prompt)])
    rows = []
    for model in BACKUPS:  # one model at a time: no swapping on an 8 GB machine
        for pid, prompt, n in PROMPTS:
            base = ChatParams(model, [Message("user", prompt)], 0.0, 600)
            if not grade(refs[pid], n)["valid"]:
                continue  # the writer itself got it wrong: nothing to resume cleanly
            restart = await call(("restart", pid, model), model, base.messages)
            for frac in CUTS:
                prefix = cut_at(refs[pid], frac)
                cases = {"error": prefix, "restart": prefix + restart}
                for strat in ("prefill", "instruct"):
                    cp = continuation_params(base, prefix, strat)
                    cont = await call((strat, pid, model, frac), model, cp.messages)
                    k = overlap(prefix, cont)
                    cases[f"{strat} raw"] = prefix + cont
                    cases[f"{strat} + dedupe"] = prefix + cont[k:]
                    rows.append({"model": model, "pid": pid, "frac": frac,
                                 "strategy": f"{strat} + dedupe", "trimmed": k})
                for strat, text in cases.items():
                    rows.append({"model": model, "pid": pid, "frac": frac, "strategy": strat,
                                 **grade(text, n)})
    out.close()
    await p.aclose()
    report(rows, refs)


def report(rows: list[dict], refs: dict) -> None:
    graded = [r for r in rows if "valid" in r]
    order = ["error", "restart", "prefill raw", "prefill + dedupe", "instruct raw",
             "instruct + dedupe"]
    valid_refs = sum(grade(refs[p], n)["valid"] for p, _, n in PROMPTS)
    md = [
        "# Stream resume: is the answer clean after a mid-stream failure?",
        "",
        (f"Generated by `bench/stream_resume_eval.py` (local Ollama, $0). {len(PROMPTS)} prompts "
         f"asking for numbered lists of known length; the writer ({WRITER}) got {valid_refs} "
         "right, and only those are used. Each answer is cut at 30% and 65% and continued by a "
         "backup model. **Clean** = numbered exactly 1..N, no repeats, no gaps, no restart."),
        "",
        "| strategy | backup model | clean | repeated items | restarted | gaps |",
        "|---|---|---|---|---|---|",
    ]
    for strat in order:
        for model in BACKUPS:
            rs = [r for r in graded if r["strategy"] == strat and r["model"] == model]
            if not rs:
                continue
            n = len(rs)

            def pc(k, rs=rs, n=n):
                return f"{sum(r[k] for r in rs)}/{n}"

            md.append(f"| {strat} | {model} | **{pc('valid')}** | {pc('dup')} | "
                      f"{pc('restart')} | {pc('gap')} |")
    trims = [r["trimmed"] for r in rows if "trimmed" in r]
    md += [
        "",
        (f"Seam dedupe trimmed repeated text in {sum(t > 0 for t in trims)} of {len(trims)} "
         f"continuations (median trim when it fired: "
         f"{sorted(t for t in trims if t)[len([t for t in trims if t]) // 2] if any(trims) else 0}"
         " chars)."),
        "",
        ("`error` is what ADR 0001 does by default: the client gets an honest partial answer "
         "and an error event, and is never clean by this measure. `restart` is why ADR 0001 "
         "forbids silent restarts. The gateway's opt-in setting is `stream_resume: prefill | "
         "instruct` on a route (ADR 0008)."),
    ]
    OUT.mkdir(exist_ok=True)
    (OUT / "stream-resume.md").write_text("\n".join(md) + "\n")
    print("\n".join(md))


if __name__ == "__main__":
    asyncio.run(main())
