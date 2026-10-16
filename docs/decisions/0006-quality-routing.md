# ADR 0006: Quality-aware routing

**Status:** accepted (Phase 6). The evidence is `bench/results/routing-eval.md`: 60 prompts,
llama3.2:1b as cheap, mistral 7B (Q4) as strong, graded against reference answers.

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
- **What the eval found:**
  1. *There was no quality gap to route on.* Cheap 82% [72–90], strong 80% [70–90], at 0.08×
     vs 1× the cost. The 18-month-old 7B model was not better than a current 1B one on these
     tasks. Their errors differ, though: an oracle reaches 93%.
  2. *The free checks almost never fire* (1 of 60). The small model's errors are fluent and
     wrong, not visibly broken. The checks are a safety net for breakage, not a quality
     router.
  3. *Self-consistency* (3 samples at temperature 0.7, escalate on any disagreement) is the
     only check above the random-escalation line (+8 points, 88% at 0.78×). It caught all 11
     wrong answers, at 44% precision. But it is inside the noise at n=60, and it adds ~5s.
     It also depends on comparing extracted answers, which this eval could do because its
     prompts ask for a "Final answer:" line; a gateway can't, for arbitrary text.
     **Not built into the gateway.**
  4. *The 1B judge* said NO to 10 of 11 wrong answers but also to 42 of 49 right ones, so it
     barely discriminates. **Not built.**
- **Eval design choices worth keeping:**
  - Reference answers, not an LLM grader.
  - A by-hand audit of all 120 grades: the auto-grader was wrong on 11. Number words were
    fixed in the grader; the rest are listed with reasons in
    `data/routing_grade_overrides.json`.
  - Bootstrap intervals.
  - Cost from real token counts.
  - A random-escalation line as the baseline every check must beat.
- **Next, if wanted:** re-run with a strong model that actually is stronger (e.g. a current 7–8B
  model). The collector takes the model names as constants and resumes where it stopped.
