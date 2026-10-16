# Cache eval set

`cache_pairs.jsonl`: one JSON object per line.

| field | meaning |
|---|---|
| `a`, `b` | two user prompts |
| `same` | **true** if a correct answer to `a` is a correct answer to `b`. That is the cache's question, not "are they about the same topic" |
| `category` | `paraphrase` (same), `hard_negative` (almost the same words, different answer), `related` (same topic, different question), `unrelated` |
| `split` | `dev` (used to choose the threshold) or `test` (read once, to report) |

**Status: first draft, labelled by Claude while building Phase 5. Review every `same` before
trusting the numbers.** Some to look at:

- `"What is 15% of 200?"` / `"What is 20% of 150?"`: both are 30, but the cached text would say
  the wrong thing. Labelled different.
- `"What is the Great Wall of China?"` / `"How long is the Great Wall of China?"`: an overview
  answer probably mentions the length. Labelled different.

Known bias: hard negatives were written as minimal pairs and paraphrases as rewrites, which
makes the lexical guard look better than it would on real traffic. The best fix is pairs taken
from real logs. Add rows (keep `split` balanced), then run `python bench/cache_eval.py`.
