# Phase 6 notes

Read `bench/results/routing-eval.md` and ADR 0006 first. `python bench/routing_eval.py` re-runs
the analysis instantly. `bench/routing_collect.py` re-runs the models (~35 min, resumable).

- Why does escalating a *random* p% of requests land exactly on the straight line between
  always-cheap and always-strong? Why is that the right baseline for every check?
- The oracle reaches 93% although both models alone score ~80%. What does that say about how
  their errors overlap, and what would a perfect check be worth?
- The free heuristics fired once in 60. Which kinds of traffic *would* make them fire?
- Self-consistency caught every wrong answer but fired 25 times to do it. What does each false
  alarm cost, in money and in latency?
- The 1B judge said NO to 42 of 49 correct answers. Try changing the judge prompt (order,
  wording, asking for a reason first). Does its bias move?
- The auto-grader was wrong on 11 of 120 grades, in both directions. If you hadn't audited,
  which policy would have looked better than it is?
- With 60 prompts every interval is about ±10 points. How many prompts would you need to tell
  82% from 88% apart?
