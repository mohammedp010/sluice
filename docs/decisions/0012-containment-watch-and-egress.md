# ADR 0012: Containment, part 3: notice, react, and keep the record honest

**Status:** accepted (Phase 9). Code: `src/sluice/guard/secrets.py`, `src/sluice/guard/watch.py`,
`src/sluice/mcp/proxy.py`, `src/sluice/mcp/audit.py`, `src/sluice/api/app.py`,
`src/sluice/limits/state.py`, `src/sluice/admin.py`, `src/sluice/report.py`. Tests:
`tests/test_egress_guard.py`, `tests/test_watch.py`, `tests/test_audit_chain.py`. Evals:
`bench/egress_eval.py`, `bench/security_eval.py`. Runbook: `docs/runbook-key-compromise.md`.

**Context:** parts 1 and 2 (ADRs 0010, 0011) made a theft worth less without detecting
anything. This part does the detecting, for three gaps that remained:
- **Data through allowed tools.** Tool poisoning sends secrets out through tools the agent is
  allowed to use. Deny-by-default says *which* tools; nothing looked at *what* went through
  them.
- **Valid keys behaving badly.** A stolen key passes every check. In the 2026 intrusions
  (CrowdStrike, Sysdig's JadePuffer) agents ran an intrusion end to end in 12-20 minutes, and
  LLMjacking drains a stolen key fast. A response that waits for a human arrives too late.
- **The audit log.** It had no retention and no way to tell that a row had been changed.

**Decision: secret egress guard**
- `guard/secrets.py` finds two kinds of secret:
  - known formats: OpenAI, Anthropic, Sluice, Stripe, AWS, GitHub, GitLab, Slack, npm and
    Google keys, PEM private keys, JWTs, and URLs with a password in them
  - long random-looking runs with a hint word ("key", "token", "secret", "password", "bearer"...)
    on the same line, within 40 characters.
- Policy, in a top-level `guard:` block:
  - `tool_args`: each `server__tool` glob is `off`, `audit` or `block`. Arguments are scanned
    before they're forwarded.
  - `tool_results`: each glob is `off`, `audit` or `redact`. Results are scanned before they go
    back to the model, `structuredContent` included.
  - `chat`: `off`, `audit` or `block` (400 `secret_in_prompt`). Prompts are scanned before they
    go to a provider.
  - The first glob that matches wins. The default is `audit` everywhere.
- A finding is recorded as a type and a position only, never the value:
  - the MCP audit's `egress` column
  - a span event
  - the `sluice.guard.findings` counter.
- A blocked call answers with the type of secret found, never the value, and feeds R5.

**Decision: behaviour watch**
- `guard/watch.py` runs five rules. Their counts live in the shared state, Redis in
  production, so every replica sees the same counts:

  | Rule | Signal | Action |
  |---|---|---|
  | R1 probe | 10 refused tool calls (denied, unknown, pending, quarantined) in 5 min | suspend the key |
  | R2 burn rate | half the daily budget spent in 10 min (keys without a budget: $10) | suspend the key |
  | R3 new network | the key's first request from a /24 (IPv6 /64) not seen for 30 days | alert |
  | R4 scanner | a bearer value that isn't key-shaped (quotes, spaces, SQL), or 20 failed key checks from one IP in a minute | refuse that IP for 15 min |
  | R5 egress | the egress guard blocked a tool call | suspend the key |

- **Automatic actions can be undone.** A suspension is lifted with `unsuspend-key`, and IP
  blocks expire. Revoking stays a human decision.
- `watch.enforce: false` turns every action into an alert, for measuring before trusting it.
  Every threshold is in `watch:`.
- A malformed bearer value never reaches the key lookup. An empty `Bearer ` header counts as no
  key, not as a probe, since load balancers send one.
- **Alerts** go to:
  - a span event
  - a JSON line on the `sluice.security` logger
  - the `sluice.security.events` counter
  - the admin API (`/v1/sluice/admin/security`, last 200)
  - an optional Slack-compatible webhook (`SLUICE_ALERT_WEBHOOK`), sent in the background so an
    alert never slows a request.
- **Tool-call limits:** `tool_rpm` per key (default 60) is a token bucket like the chat limits,
  shared by a rotation's lineage. This closes ADR 0009's open gap.
- **Lockdown:**
  - `sluice.admin lockdown [--team X]` suspends every active key in scope.
  - Without `--team` it also sets a flag in the shared database that turns paid providers off
    on every replica, even with `SLUICE_ALLOW_PAID=1`.
  - `--lift` undoes only what lockdown did. Keys suspended for other reasons stay suspended.
- **Redis down still fails closed (503) on every keyed route**, tool calls included. This
  answers the plan's open question 4: without shared counts the watch can't see, so the gateway
  refuses rather than running blind.

**Decision: audit log**
- **Hash chain:** each MCP audit row stores the previous row's hash and its own:
  `sha256(prev + row as canonical JSON)`.
  - `report --verify-audit` walks the chain. It exits 1 and names the first bad row if a row
    was edited, or deleted from the middle.
  - Writes take `BEGIN IMMEDIATE`, so two replicas sharing one database file can't fork the
    chain.
  - Older databases are chained once, when they're first opened.
- **Retention:** `mcp.audit_retention_days` prunes old rows hourly; `admin prune-audit --days`
  does it by hand. Each prune records a checkpoint (the last deleted row's hash), so the rows
  that remain still verify.
- `report --security` summarises what containment left on disk: keys that aren't active,
  refused tool calls, secrets seen, tools held, the lockdown flag. `report` and `admin` both read
  their database paths from `$SLUICE_POLICY`.
- **The incident runbook** (`docs/runbook-key-compromise.md`) strings these commands together.

**Measured:**
- **Egress guard** (`bench/results/egress-eval.md`):
  - 560 of 560 generated fake secrets found, across 14 formats in six kinds of surrounding text.
  - 0 false positives in 4.4 MB of this repository's real text: source, docs, the git log,
    UUIDs, sha256 digests, and screenshots as base64.
  - About 140 µs per KB.
  - It is beaten by encoding: base64-encoded, spaced and reversed secrets were found 0 of 60
    times each, and secrets split in two 20 of 60.
  - The eval's first run found two detector bugs, fixed with regression tests:
    - `PASSWORD=<secret>` hid its own hint word, so 12 of 40 hinted secrets were missed.
    - Hints were read across line breaks, so three git SHAs were flagged because the commit
      subject on the line before said "Keys".
- **Watch** (`bench/results/security-eval.md`, in-process, mock upstream, virtual clock):
  - A stolen key with a $50 daily budget, used 10 times a second from a new network: without
    the watch it got $40 before the budget stopped it. With the watch it got $23.20 in 5.9 s,
    then R2 suspended it. R3 alerted on its first request.
  - Guessing tool names: 10 guesses, then the key was suspended.
  - A SQL-injection bearer value: the IP was blocked on the first request. The real key still
    worked from other addresses.
  - Key-shaped guesses: the IP was blocked after 20.
  - A credential in a mail tool's arguments: 0 calls forwarded, the key suspended, and the
    secret not in the audit.
  - A modelled normal day (40 keys, 36k chat requests and tool calls, with second offices,
    roaming laptops, mistyped tool names and a stale deployment retrying all day): 0 false
    suspensions, 0 IP blocks, 200 R3 alerts.
- **Overhead,** alternating rounds of 1,800 requests each:
  - Shared state in memory: +0.03 ms at p50.
  - Redis: +0.4 ms at p50, from two extra round trips (the IP-block lookup and the burn
    counter).
  - Each audit write costs ~170 µs more with the hash chain, on the worker thread.
- 240 tests pass with Redis.

**Trade-offs and limits:**
- **R2 lets half a budget go before it acts.** A stolen key still spent $23 of $50 in the
  eval. The budget remains the hard cap. Lowering `burn_fraction` catches theft sooner, but
  also catches legitimate bursts sooner.
- **The normal day is a model, not real traffic.** Known false-positive mechanisms:
  - R1: a model that keeps inventing a tool name.
  - R2: a batch job that spends half its budget in 10 minutes.
  - R4: many clients behind one NAT or untrusted proxy, one of them retrying with a stale key.
    List the proxy in `auth.trusted_proxies`.
  - Start with `enforce: false` if any of these describe your traffic.
- **R3 is noisy:** every roaming laptop raises one. Treat it as a log, not a pager.
- **The windows are fixed, not sliding.** A burst that straddles a window boundary counts in
  two halves.
- **The detector is a heuristic.** An encoded, split or paraphrased secret gets through.
  Streamed chat responses aren't scanned.
- **The hash chain lives in the database it protects.** An attacker with write access can
  rebuild it. Only a head hash recorded elsewhere catches that; signed checkpoints to an
  append-only store are the production path.
- **Alerts in memory don't survive a restart.** The log lines are the durable record.
- **Not built:**
  - R6 (tool-call burst against a 7-day p95 baseline). It needs per-minute history the gateway
    doesn't keep; `tool_rpm` is the hard cap meanwhile.
  - Pushing suspensions to replicas over pub/sub. Replicas share the key database, so a
    suspension applies on each one's next lookup.
