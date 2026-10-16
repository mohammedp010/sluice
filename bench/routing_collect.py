"""Phase 6, step 1 of 2: run the models once and keep everything (slow; ~35 min on an M2).

    python bench/routing_collect.py        # resumable: re-running skips calls already saved

Per prompt in bench/data/routing_prompts.jsonl:
  cheap  greedy answer (temperature 0)
  cheap  3 samples at temperature 0.7      -> self-consistency signal
  cheap  YES/NO "is this answer correct?"  -> small-model judge signal
  strong greedy answer                      -> what escalation would have bought
Calls are grouped by model so an 8 GB machine never holds both models at once.
Analysis is separate (bench/routing_eval.py) and instant, so it can be re-run freely.
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from sluice.providers.base import ChatParams, Message
from sluice.providers.openai_compat import OpenAICompatProvider

ROOT = Path(__file__).parent
PROMPTS = ROOT / "data/routing_prompts.jsonl"
RUNS = ROOT / "data/routing_runs.jsonl"
CHEAP, STRONG = "llama3.2:1b", "mistral:latest"
SAMPLES = 3
MAX_TOKENS = 256

SYSTEM_TEXT = ("Answer the question. Think briefly if needed, then end with a final line of the "
               "form 'Final answer: <answer>'.")
SYSTEM_JSON = "Respond with only a JSON object, no other text."
JUDGE = ("Question: {q}\n\nProposed answer: {a}\n\nIs the proposed answer correct? "
         "Reply with exactly one word: YES or NO.")


def messages(item: dict) -> list[Message]:
    system = SYSTEM_JSON if item["kind"] == "json" else SYSTEM_TEXT
    return [Message("system", system), Message("user", item["prompt"])]


async def call(p: OpenAICompatProvider, model: str, msgs: list[Message], temperature: float,
               max_tokens: int) -> dict:
    t0 = time.perf_counter()
    c = await p.complete(ChatParams(model, msgs, temperature, max_tokens))
    return {"model": model, "text": c.content, "finish_reason": c.finish_reason,
            "prompt_tokens": c.usage.prompt_tokens, "completion_tokens": c.usage.completion_tokens,
            "ms": round((time.perf_counter() - t0) * 1000, 1)}


async def main():
    items = [json.loads(line) for line in PROMPTS.read_text().splitlines()]
    done = {}
    if RUNS.exists():
        for line in RUNS.read_text().splitlines():
            r = json.loads(line)
            done[(r["id"], r["role"], r["mode"], r["n"])] = r
    p = OpenAICompatProvider("ollama", "http://localhost:11434/v1", timeout_s=300)
    out = RUNS.open("a")

    async def run(item, role, mode, n, model, msgs, temperature, max_tokens):
        key = (item["id"], role, mode, n)
        if key not in done:
            rec = {"id": item["id"], "role": role, "mode": mode, "n": n,
                   **await call(p, model, msgs, temperature, max_tokens)}
            out.write(json.dumps(rec) + "\n")
            out.flush()
            done[key] = rec
            print(f"{item['id']} {role:6} {mode:6} {n} {rec['ms']:>8.0f}ms  "
                  f"{rec['text'][-60:]!r}", flush=True)
        return done[key]

    for item in items:  # all cheap-model work first
        greedy = await run(item, "cheap", "greedy", 0, CHEAP, messages(item), 0.0, MAX_TOKENS)
        for n in range(SAMPLES):
            await run(item, "cheap", "sample", n, CHEAP, messages(item), 0.7, MAX_TOKENS)
        judge = [Message("user", JUDGE.format(q=item["prompt"], a=greedy["text"]))]
        await run(item, "cheap", "judge", 0, CHEAP, judge, 0.0, 3)
    for item in items:  # then the strong model
        await run(item, "strong", "greedy", 0, STRONG, messages(item), 0.0, MAX_TOKENS)
    out.close()
    await p.aclose()


if __name__ == "__main__":
    asyncio.run(main())
