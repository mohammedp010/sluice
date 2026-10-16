# Phase 5 notes

Read `bench/results/cache-eval.md` and ADR 0005 first. Then review the labels in
`bench/data/cache_pairs.jsonl` and re-run `python bench/cache_eval.py`.

- Why does normalising vectors to length 1 turn cosine similarity into a dot product? What does
  `MemoryVectorStore.search` save by doing it once at insert time?
- "5 km to miles" vs "5 miles to km" scores 0.997. What does that tell you about what an
  embedding represents?
- Dev showed zero false hits; test showed 3 of 33. With 81 dev pairs, how surprised should you
  be? (Look up the rule of three.)
- The guard can only remove hits. Why does that make it safe to add even if it's tuned badly?
  What *does* a badly tuned guard cost?
- The eval rewards a lower threshold. Why is choosing the threshold on the test split, after
  seeing the 3 false hits, cheating?
- Every cached-route request pays ~11ms of embedding, even a miss. At what repeat rate does the
  cache start saving time overall, given an average upstream latency L?
- Scope uses the team, not the key. What breaks if two teams share a team name, or if a
  prompt contains per-user data?
