# Chaos results

Mock upstreams, in-process, 200 requests at concurrency 20 per scenario. Machine-specific; rerun to compare.

| scenario | success | primary | backup | p50 ms | p95 ms |
|---|---|---|---|---|---|
| baseline (healthy) | 100.0% | 200 | 0 | 21.2 | 22.2 |
| primary down (500s) | 100.0% | 0 | 200 | 20.8 | 88.0 |
| primary rate-limited (429s) | 100.0% | 0 | 200 | 21.0 | 103.3 |
| primary flaky (30% 500s) | 100.0% | 179 | 21 | 20.8 | 126.4 |
| primary slow (2s, timeout 0.25s) | 100.0% | 0 | 200 | 20.6 | 320.2 |

```json
{
  "_recovery": {
    "breaker_cooldown_s": 1.0,
    "seconds_until_primary_serves_again": 0.97
  },
  "_stream_cut_mid_stream": {
    "http_status": 200,
    "got_done_marker": false,
    "got_error_event": true
  }
}
```
