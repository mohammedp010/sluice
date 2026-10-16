# ADR 0008: Opt-in stream resume ("restart with prefix")

**Status:** accepted (Phase 8). Evidence: `bench/results/stream-resume.md`
(`bench/stream_resume_eval.py`, local Ollama, $0).

**Context:** ADR 0001 ends a stream that dies after its first byte with an error event. That's
honest, but the client is left with half an answer. A *silent restart* on another provider is
worse: the eval measured 0/30 clean answers that way, with nearly every one duplicating items.

**Decision:** a route may set `stream_resume`.
- `off` (the default) keeps ADR 0001.
- `prefill` continues on the next provider in the chain (never the one that just died), once
  per request. The text the client already has goes in as a trailing assistant message, so the
  model continues its own words.
- `instruct` sends that text, then a user turn asking to continue. It is kept for providers
  that don't honour prefill.
- Seam dedupe: the start of the continuation is held back (up to the length of the prefix, max
  2,000 chars) and any part that repeats the end of what was sent is cut (minimum 8 chars, so
  coincidences like a shared newline stay). A continuation that starts over is cut entirely.
- The client's `max_tokens` covers both segments.
- Both segments are billed: the cut one estimated, the continuation as reported.
- The cut provider gets one breaker strike.
- The trace gets a `sluice.stream_resume` event; the ledger outcome is `resumed`.

**Evidence:** 15 numbered-list answers, each cut at 30% and 65%; clean means numbered exactly
1..N.

| strategy | same model (qwen2.5:3b) | different model (llama3.2:1b) |
|---|---|---|
| error (ADR 0001) | 0/30 | 0/30 |
| silent restart | 0/30, 30 duplicated | 0/30, 28 duplicated |
| **prefill** | **30/30** | **29/30** |
| instruct | 17/30 | 10/30 |

- **instruct's failures share one cause.** The cut fell right after an item number (`"6. "`);
  told to "continue", the model started a new line, giving `6. 7. Giraffe` (item 6 broken,
  item 7 repeated). Prefill continues the characters themselves.
- **The one prefill failure came from a garbled reference answer**, which the 1B model
  imitated. It's a flawed input, not a seam error.
- **Seam dedupe fired 2 times in 120, both under instruct, both correct.** Under prefill it
  never fired. It's cheap insurance, kept.

**Limits:**
- **Prefill depends on the backup provider treating a trailing assistant message as text to
  continue.** Ollama does, as tested here. OpenAI's API, as far as I know, does not, and some
  other OpenAI-compatible servers need a flag. Use `prefill` only when every target that could
  take over supports it; otherwise use `instruct`, or leave it `off`.
- **The client is not told a resume happened.** The OpenAI stream format has no field for it,
  and the headers were sent long before. It's visible in the trace and the ledger only.
- **Lists are the friendliest case for measuring seams** (they're countable). Prose may hide
  seam errors this eval can't see.
- **One resume per request.** A second death ends the stream with the original error event.
