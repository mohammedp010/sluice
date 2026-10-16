# ADR 0003: Rate limits, budgets and failure policy

**Status:** accepted (Phase 3).

- **Algorithm:** token bucket per (key, dimension). Capacity = the per-minute limit, refill =
  limit/60 per second. Chosen over sliding-window-log because it is O(1) in memory and lets us
  *adjust* the balance after the fact. Downside: allows a burst of up to `limit` at once.
- **Two buckets:** RPM (cost 1) then TPM (cost = estimated tokens). A request rejected by TPM has
  already spent its RPM token (standard behaviour; keeps the check order cheap).
- **Estimate then reconcile:** estimate = chars/4 prompt + `max_tokens` (or a default). After the
  call, the bucket is adjusted by `estimate - actual`; failed calls refund everything; streams
  reconcile in a `finally`, so client disconnects also settle (billing what was already emitted).
- **Atomicity:** each Redis read-modify-write is one Lua script using Redis' own clock (`TIME`),
  so replicas never disagree on "now". The in-memory implementation mirrors it and both run
  the same behaviour tests.
- **Budgets:** spend kept as integer micro-USD per key per UTC day/month, from a static price table
  on each route target. Checked *before* a call, so concurrent in-flight requests can overshoot
  a cap slightly. Accepted: exact enforcement would need pre-reserving worst-case cost.
- **Failure policy:** if Redis is unreachable the gateway returns 503 (fail closed). Silently
  allowing traffic would mean unmetered spend.
- **Money kill switch:** providers marked `paid: true` are skipped unless `SLUICE_ALLOW_PAID=1`,
  and then only for `paid_daily_call_cap` calls/day (shared counter).
- **Keys:** `sk-sluice-` + 256 random bits; only the SHA-256 is stored; shown once at creation.
- **Known gaps:** key lookup hits SQLite on every request (fine at this scale, cache later);
  admin is CLI only; body cap relies on Content-Length (chunked uploads are not capped).
