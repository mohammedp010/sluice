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

# Tool description lint sets

`tool_descriptions_benign.jsonl`: every tool and parameter description in seven MCP reference
servers (`everything`, `fetch`, `filesystem`, `git`, `memory`, `sequentialthinking`, `time`) from
[modelcontextprotocol/servers](https://github.com/modelcontextprotocol/servers) at commit
`194cc86e3ae6`, Apache-2.0 / MIT. Extracted from source by `bench/tool_corpus.py` without running
or importing any of it. `kind` is `tool` (the tool's own description) or `param` (a parameter,
schema or form field description); `{name}` marks an interpolation the extractor couldn't
resolve.

`tool_descriptions_poisoned.jsonl`: 32 synthetic strings written for this eval after published
tool-poisoning patterns (Invariant Labs' `<IMPORTANT>` disclosure, tool shadowing, MCPTox's
paradigms). None is from a real attack. `category`: `explicit`, `hidden` (invisible characters,
payload pushed out of view), `shadowing` (about another server's tool), `param` (payload in a
parameter description), `subtle` (innocent wording, written to get past a keyword lint).
Invisible characters are stored as JSON escapes.

Known bias: the poisoned set was written by whoever wrote the lint, after reading the same
write-ups. Treat `explicit` and `hidden` recall as an upper bound.
