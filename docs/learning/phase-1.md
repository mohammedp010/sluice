# Phase 1 notes

(Fill in after the phase: what surprised me, what I'd do differently.)

Questions to answer in your own words:
- Why must the first chunk be fetched before returning StreamingResponse?
- What happens to the upstream request when the client disconnects mid-stream?
- Why does the adapter use `stream_options.include_usage`?
