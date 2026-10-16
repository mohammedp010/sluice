# Phase 8 notes

Read ADR 0008, ADR 0009 and `bench/results/stream-resume.md` first.

**Stream resume**
- Why does "silent restart" duplicate almost every time, while prefill almost never does? What
  does the model see differently in each case?
- instruct failed whenever the cut fell right after `"6. "`. Why does an instruction ("continue
  where you stopped") lose information that a prefix keeps?
- Seam dedupe never fired under prefill. Is it still worth keeping? What would make it fire?
- The client is never told its answer was stitched from two models. Is that acceptable? What
  would you add to the stream if the OpenAI format allowed it?
- This eval uses numbered lists because seams are countable there. Design a check for seams in
  prose.

**MCP proxy**
- Why does `tools/call` for a denied tool return the same error as for a missing one? What
  could a key learn otherwise?
- The audit stores a hash of the arguments, not the arguments. Which questions can you still
  answer from it, and which can't you?
- Why does each upstream connection need its own task? Reproduce the "cancel scope in a
  different task" error by opening a connection inside a request handler.
- Stateless (no session id) lets any replica serve any request. What MCP features does that
  rule out?
- Tool calls aren't rate-limited. Add an RPM bucket for them, reusing `limits/state.py`. Which
  key should it share with chat requests, if any?
