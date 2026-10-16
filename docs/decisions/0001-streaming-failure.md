# ADR 0001: Streaming failure behaviour

**Status:** accepted (Phase 1), revisit in Phase 2.

**Decision:** the gateway pulls the first upstream chunk before sending any response bytes.
- Failure before the first chunk -> proper HTTP error (502/504). Phase 2 will fail over here.
- Failure after streaming began -> emit one SSE `data: {"error": {...}}` event and close
  without `[DONE]`, so clients can tell the stream did not finish.

**Why:** once the 200 status line and first bytes are sent they cannot be taken back. Silently
restarting on another provider would splice two different answers together.
