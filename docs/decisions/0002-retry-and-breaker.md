# ADR 0002: Retry, failover and circuit-breaker semantics

**Status:** accepted (Phase 2).

- **Retryable:** no HTTP answer (connect error, timeout, stream cut), 408, 429, any 5xx, and
  anything unreadable (a 200 with a malformed body, an in-band SSE `error` event, a stream that
  closes without `[DONE]`).
  **Fail over, but don't retry:** 401, 403, 404. These mean *our* provider key or model mapping is
  wrong for that provider; the next target may be fine. They count against the breaker and are
  never forwarded to the client (its SDK would think the client's own key was bad).
  **Neither:** other 4xx. They are returned to the client as-is; retrying or failing over
  repeats the same mistake, and they do not count against the provider's breaker.
- **Probe release:** if a half-open probe ends with no health verdict (non-retryable 4xx,
  cancellation, unexpected exception) the probe slot is released, otherwise the breaker would
  reject everything forever.
- **Order:** per target, `1 + retries` attempts with full-jitter exponential backoff, then fail
  over to the next target. The route `deadline_s` caps total time across everything.
- **Timeouts:** per attempt; for streams it is time-to-first-chunk only.
- **Breaker:** per provider (health belongs to the provider, not the route). Consecutive
  failures >= threshold -> open; after cooldown one half-open probe; success closes, failure reopens.
  Open providers are skipped without being called.
- **Streams:** failover only before the first chunk (ADR 0001). A mid-stream death is not retried
  but does count as a breaker failure.
- **Timeouts are the adapter's, not cancellation (ADR 0007):** cancelling httpx mid-connect
  leaked pool slots until a provider was dead for good. Adapters that set `enforces_timeout`
  get the budget as `ChatParams.timeout_s`, and the executor only cancels as a backstop
  (budget + 1 s). httpx's read timeout also bounds the silence between stream chunks.
- **Known gaps:** a 429 `Retry-After` is not honoured yet; breaker state is per process
  (Phase 3 moves shared state to Redis if needed).
