# ADR 0006: Quality-aware routing

**Status:** accepted (Phase 6). The evidence is two runs of the same 60 reference-graded prompts,
with llama3.2:1b as the cheap model:
`bench/results/routing-eval-mistral.md` (strong = mistral 7B, Q4) and
`bench/results/routing-eval-qwen2.5.md` (strong = qwen2.5:3b).

- **Mechanism:** a route can name a stronger `escalate` route and the checks that trigger it
  (`escalate_on`).
  - A non-streamed answer that fails a check is re-asked on the stronger route.
  - The client gets the second answer. Both calls are billed and counted against TPM.
  - Headers say what happened: `x-sluice-escalated: refusal`, or `...;failed`.
  - If the stronger route also fails, the cheap answer is returned rather than an error.
  - An answer that failed a check is never cached.
- **Streams skip the check.** A streamed answer can't be inspected before it is sent (ADR 0001),
  so on an escalating route streams go straight to the stronger route. That keeps quality, but
  gives up the saving for streaming clients.
- **Checks in the gateway are the free ones only:** empty, truncated, refusal, invalid JSON
  (when `response_format` asked for JSON; that field is now forwarded upstream) and repetition
  loops. They read the answer and cost microseconds.
- **Pricing in the eval:** real token counts, priced in proportion to parameter count (1B at
  $0.15/$0.60 per Mtok in/out, so 3B is 3× and 7B is 7×). Only ratios matter: costs are shown
  relative to always-strong.
- **What the eval found:**

  | strong model | always cheap | always strong | best check | perfect check |
  |---|---|---|---|---|
  | mistral 7B | 82% at 0.20× | 80% | self-consistency 88% at 1.25× | 93% at 0.38× |
  | qwen2.5:3b | 82% at 0.29× | 85% | self-consistency 88% at 1.64× | 93% at 0.46× |

  1. *No usable quality gap.*
     - mistral 7B (18 months old) scored no better than the 1B model.
     - qwen2.5:3b was better on easy prompts (30/30 vs 28/30) but **tied on hard ones (21/30
       each)**.
     - The hard set is dominated by letter-level tasks (counting the r's in "strawberry",
       spelling backwards) and multi-digit arithmetic. Small models fail these regardless of
       size, because tokenisation hides letters.
     - With every 95% interval about ±10 points, all three models are statistically tied.
  2. *The models make different mistakes.* A perfect check (escalate exactly when the cheap
     answer is wrong) would reach 93% at under half the cost. The prize is real; finding a
     check is the problem.
  3. *The free checks almost never fire* (1 of 60). The small model's errors are fluent and
     wrong, not visibly broken. The checks are a safety net for breakage, not a quality
     router.
  4. *Self-consistency* (3 samples at temperature 0.7, escalate on any disagreement) caught all
     11 cheap errors, but fired 25 times to do it.
     - Under size-proportional prices it costs more than always using the strong model (1.25×,
       1.64×).
     - Against random escalation it gains +9 points with mistral, but **+0 with qwen2.5:3b**.
     - It only worked here because the eval prompts ask for a "Final answer:" line, so answers
       can be compared. A gateway can't do that for arbitrary text.
     - **Not built into the gateway.**
  5. *The 1B judge* said NO to 10 of 11 wrong answers, but also to 42 of 49 right ones. It
     barely discriminates. **Not built.**
- **Conclusion:** on this workload and hardware, cheap-first routing doesn't pay; always-cheap
  is the best cost/quality point. Escalation stays in the gateway as a free safety net against
  broken answers. A real quality router needs two things this setup lacked: a strong model
  that is clearly stronger on *your* traffic, and a check that sees fluent errors.
- **Eval design choices worth keeping:**
  - Reference answers, not an LLM grader.
  - A by-hand audit of every grade: the auto-grader was wrong on 11 of 120 in the first run
    and 2 of 60 in the second. Number words were fixed in the grader; the rest are listed
    with reasons in `data/routing_grade_overrides.json`.
  - Bootstrap intervals.
  - Real token counts.
  - A random-escalation line as the baseline every check must beat.
