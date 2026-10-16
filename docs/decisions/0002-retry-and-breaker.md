# ADR 0002: Retry, failover and circuit-breaker semantics

**Status:** accepted (Phase 2).

- **Retryable:** no HTTP answer (connect error, timeout, stream cut), 408, 429, any 5xx.
  **Not retryable:** other 4xx. They are returned to the client as-is; retrying or failing over
  repeats the same mistake, and they do not count against the provider's breaker.
- **Order:** per target, `1 + retries` attempts with full-jitter exponential backoff, then fail
  over to the next target. The route `deadline_s` caps total time across everything.
- **Timeouts:** per attempt; for streams it is time-to-first-chunk only.
- **Breaker:** per provider (health belongs to the provider, not the route). Consecutive
  failures >= threshold -> open; after cooldown one half-open probe; success closes, failure reopens.
  Open providers are skipped without being called.
- **Streams:** failover only before the first chunk (ADR 0001). A mid-stream death is not retried
  but does count as a breaker failure.
- **Known gaps:** a 429 `Retry-After` is not honoured yet; no idle timeout between stream chunks;
  breaker state is per process (Phase 3 moves shared state to Redis if needed).
