# ADR 0011: Containment, part 2: keys that expire, tools that can't change quietly

**Status:** accepted (Phase 9). Code: `src/sluice/auth/keys.py`, `src/sluice/admin.py`,
`src/sluice/mcp/pins.py`, `src/sluice/mcp/upstream.py`, `src/sluice/mcp/proxy.py`,
`src/sluice/api/app.py`, `src/sluice/config.py`. Tests: `tests/test_key_containment.py`,
`tests/test_tool_pins.py`.

**Context:** part 1 (ADR 0010) kept secrets out of reach and closed the usual doors. This part
makes a single theft worth less, with no detection logic:
- A leaked `sk-sluice-` key used to work until someone noticed. After Railway's October breach,
  the advice was short-lived credentials, network allowlists, and a way to revoke many at once.
  Railway built that last one *after* the incident.
- MCP clients apply tool metadata as soon as a server sends it. A server can pass review with
  an honest description and later swap in one that tells the agent to read `~/.ssh` and pass it
  along (a "rug pull"). Every call that follows looks authorised.

**Decision: keys**
- A key has a status: `active`, `suspended` (reversible, for automatic responses later) or
  `revoked` (final).
- **Expiry:** `create-key --ttl 30d`, or the policy's `auth.key_ttl` when `--ttl` isn't given.
  `--no-expiry` opts out. `policies/compose.yaml` sets 90 days.
- **Network binding:** `create-key --cidrs 10.0.0.0/8,...`. The caller's address is the TCP peer,
  unless the peer is in `auth.trusted_proxies`. Then `X-Forwarded-For` is read right to left,
  skipping trusted hops, and the first untrusted one counts. Entries further left are the
  client's to write, so they're never believed. The admin API's loopback check uses the same
  rule.
- **Rotation:** `rotate-key <id> --grace 24h` issues a new key with the same grants. The old one
  keeps working until the grace ends, and a grace never extends it.
  - The new key's lifetime is `--ttl`, else the old key's, else the policy default.
  - Both keys share a **lineage**: rate limits, budgets and the cache tenant count per lineage.
    Without that, the grace overlap would double the key's limits and reset today's spend.
  - A suspended key can be rotated (the incident case: a new secret for a service you trust)
    and stays suspended. A revoked key can't be rotated.
- **Bulk revoke:** `revoke --team X`, `--created-before 2026-10-21`, `--all`. All the filters
  given must match.
- **Same answer for every refusal:** missing, unknown, revoked, suspended, expired and
  wrong-network all return the same 401. The reason goes to the `sluice.auth.result` span
  attribute and the `sluice.auth.rejections` counter, so it's visible to operators and not to
  the caller.
- The admin CLI reads the database and default TTL from `--policy` or `$SLUICE_POLICY`, so
  `docker compose exec gateway-1 python -m sluice.admin ...` uses `/data/sluice.db`.
- Older databases are migrated in place. Revocation also still sets the old `revoked` column,
  so an older build reading the same file still refuses the key.

**Decision: tool pins**
- Every tool definition the gateway relays (the whole object, parameter schemas included) is
  hashed as canonical JSON and pinned on first sight, in `mcp_tool_pins` next to the MCP audit.
- On each refresh (`mcp.tools_ttl_s`, 60 s by default), a changed hash **quarantines** the tool:
  - it disappears from `tools/list`
  - `tools/call` answers "Unknown tool", as for a tool that doesn't exist
  - the audit records `quarantined` for each refused call
  - a `sluice.mcp.tool_quarantined` span event and a `sluice.security` warning are emitted.
- Quarantine sticks even if the server changes back. It has shown it will change under you.
- `mcp.pin_mode`:
  - `auto_first` (default) trusts a new tool, unless the lint flags it.
  - `manual` holds every new tool as `pending` until approved.
- **Review:** `python -m sluice.admin tools list | diff <tool> | approve <tool> --sha <prefix>`.
  `diff` shows the trusted definition against the one seen last, plus lint findings. `approve
  --sha` pins only if the seen definition still has that hash, so a definition that changed after
  you read the diff isn't approved by accident. Replicas pick up an approval at their next
  refresh.
- **Description lint**, over every string in the definition (keys too: parameter names carry
  instructions as well). It flags:
  - invisible characters (zero-width, bidi controls, the Unicode tag block)
  - instruction-like phrases (`<IMPORTANT>`, "ignore previous", "don't tell the user", "before
    using this tool")
  - sensitive paths (`~/.`, `.ssh`, `.env`, `.aws/`, `mcp.json`, "credentials")
  - URLs
  - references to tools on other servers: gateway-style `server__tool` names, or another
    server's tool names that contain `_` or `-`
  - descriptions over `mcp.lint_max_description` (1,024) characters.
- The UI's MCP panel lists the pins. The keys panel shows status, expiry, networks and lineage.

**Measured:**
- Rug pull, with a stdio test server that poisons its own description on the second call: the
  two calls before the change were served. After the next refresh, 5 of 5 calls were refused as
  unknown, the tool was gone from `tools/list`, and the audit recorded `quarantined` for each.
  After `tools approve`, the tool was served again on the following refresh
  (`test_rug_pull_is_quarantined_at_the_next_refresh`).
- The poisoned description also tripped the lint (instruction and sensitive-path flags). The
  repo's own eight tool descriptions trip nothing.
- Key check cost (micro-benchmark, 1,000 keys in sqlite): 9.2 µs before, 9.3 µs for a key with
  no expiry or networks, 15.6 µs with an expiry and two CIDRs.
- 195 tests pass with Redis, and ruff is clean.

**Trade-offs and limits:**
- **Detection waits for a refresh.** Between a change and the next refresh (up to
  `mcp.tools_ttl_s`), calls still reach the server. Lower the TTL to narrow that window; each
  refresh costs one `tools/list` per server.
- **A pin covers the definition, not the behaviour.** A server can keep its description and
  change what the tool does. Scanning arguments and results (C3) and watching behaviour (C5)
  are the later parts that cover some of this.
- **Trust on first use:** in `auto_first`, a brand-new tool that reads innocently is served at
  once. `manual` closes that, at the cost of approving every tool.
- **The lint is a heuristic.** Poisoning can be phrased innocently: MCPTox reports success rates
  above 60% on some models. The lint hasn't yet been measured against real third-party servers.
  Until it is, treat its findings as review prompts, not verdicts.
- **Network binding needs the real client address.** In `docker compose`, uvicorn sees the Docker
  bridge, not the host's caller. Put a proxy in front and list it in `auth.trusted_proxies`, or
  bind to the bridge range.
- **Suspension is only manual for now** (`suspend-key`). Automatic suspension is part 3.
- **Old keys don't expire on their own:** `key_ttl` applies to keys created after it is set.
  Rotate or bulk-revoke older ones.

**Not done (part 3):**
- Behaviour watch and automatic suspension (burn rate, probing, new networks, lockdown), and
  rate limits on tool calls.
- Secret scanning of tool arguments and results.
- Audit log retention and a hash chain.
- A precision and recall eval of the lint against real server descriptions and a labelled
  poisoned set.
