# ADR 0005: Semantic cache

**Status:** accepted (Phase 5). The threshold is provisional until the eval set is reviewed and
grown.

- **What is compared:** only the last user message is compared by meaning. Everything else that
  changes the answer must match exactly and forms the *scope*:
  - the tenant (the key's team, else the key itself)
  - the public model
  - `temperature` and the effective `max_tokens`
  - every earlier message, including the system prompt

  One tenant can never receive another tenant's cached answer: the scope is compared in the
  same vectorised pass as similarity.
- **A false hit is worse than a miss.** A miss costs one upstream call. A false hit confidently
  answers a different question. So the threshold comes from `bench/cache_eval.py`:
  - On the dev split, take the lowest threshold with zero false hits, then the best dev recall.
  - Report the test split once.
  - The chosen model, prefix, guard and threshold live together in `CacheConfig`. Change one,
    re-run the eval.
- **Finding: embeddings alone can't do it.** Direction swaps ("5 km to miles" vs "5 miles to
  km", "string to int" vs "int to string") score 0.99+, above most real paraphrases.
  Embeddings encode topic, and those pairs share it exactly. With zero dev false hits, every
  model's recall was 0%.
- **Lexical guard:** veto a hit when the two prompts are a minimal pair: the same words
  reordered, or word sets that differ by ≤ `guard_max_diff` (2) words. A verbatim repeat (after
  case and punctuation) is never vetoed.
  - The guard can only turn hits into misses, so a wrong guard costs hit rate, never
    correctness.
  - With it, dev recall goes from 0% to 89% at zero dev false hits.
  - On test it showed **3 false hits of 33 served (9%)**, which dev hadn't exposed. Hence
    "provisional", and `cache: false` on the Ollama route.
- **Skip rules:**
  - Routes opt in (`cache: true`).
  - `Cache-Control: no-cache` means don't serve from the cache, but do store the fresh answer.
    `no-store` means serve but don't store. Both together skip the cache entirely.
  - A temperature above `max_temperature` (default 0) skips: the client asked for variety.
  - A last message that isn't from the user skips.
  - If the embedder is down the cache fails open (skip, reason `embed_error`), because it is an
    optimisation.
- **Accounting:** a hit still costs one RPM token, because limits run before the cache. Its TPM
  estimate is refunded and it costs $0. Ledger rows carry `provider = cache`, so the hit rate
  per team is `python -m sluice.report --by provider`. The response reports the original
  answer's `usage`.
- **Streams:** a hit replays as a well-formed SSE stream. Streamed misses are stored only when
  they finish normally; interrupted or disconnected streams are never cached.
- **Store:** an in-memory ring buffer with exact search (one matrix-vector product, under 1ms at
  10k entries), FIFO eviction and a per-entry TTL. Changing the embedding model resets it.
- **Cost:** every request on a cached route pays one embedding call, hit or miss (nomic about
  11ms p50 locally). A route with little repetition pays it for nothing.
- **Known gaps:**
  - The cache is per process: two replicas don't share it. pgvector (HNSW) is the planned
    shared store and needs Docker.
  - No request coalescing: identical concurrent misses all go upstream.
  - Only the top-1 match is checked. If the guard vetoes it, a valid second-best is not tried.
  - The eval set is small and draft-labelled, and its construction flatters the guard.
