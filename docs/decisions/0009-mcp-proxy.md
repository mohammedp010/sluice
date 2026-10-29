# ADR 0009: MCP proxy with per-key tool permissions and an audit log

**Status:** accepted (Phase 8). Code: `src/sluice/mcp/`. Tests: `tests/test_mcp.py` (official
MCP SDK client → Sluice → official SDK server over stdio).

**Decision:**
- **The endpoint is written by hand:** JSON-RPC 2.0 over MCP's Streamable HTTP transport, at
  `POST /mcp`.
  - It is stateless. Every request is self-contained, with no session id and no
    server-initiated stream (`GET`/`DELETE` → 405), all of which the spec allows. So any
    replica can serve any request.
  - It answers with plain JSON, not SSE.
  - It supports `initialize`, `ping`, `tools/list` and `tools/call`. Resources and prompts are
    out of scope.
  - It speaks protocol versions 2024-11-05 to 2025-11-25, echoing the client's version when
    supported.
- **Upstream servers go through the official SDK client** (`mcp` 1.30, pinned `<2`): stdio
  commands or Streamable HTTP URLs, from `mcp.servers` in the policy.
  - Each server's connection lives in its own task. The SDK's anyio task groups must be
    entered and exited in the same task, so a request never owns a connection.
  - Connections start lazily. A server that fails to start is retried after 10 s, and its
    tools simply disappear meanwhile.
- **Names:** `<server>__<tool>`. Tools from all servers are merged into one list.
- **Permissions are deny by default:** a key sees and may call only tools matching its
  `allowed_tools` globs (`sluice.admin create-key --tools "files__read_*"`). A tool the key may
  not use is answered exactly like a tool that doesn't exist, so a key can't probe for what's
  there.
- **Audit:** every `tools/call` gets a row, including denials, unknown tools, tool errors and
  servers that are down. Each row has key, team, tool, outcome, latency, trace id, and the
  SHA-256 and size of the arguments, never the arguments themselves, since tool arguments
  carry file contents, queries and secrets. Writes run on a worker thread (ADR 0007).
  `python -m sluice.report --mcp` summarises the log.
- **Security per the spec:**
  - Callers need the same bearer virtual key as the chat API.
  - A browser `Origin` not in `mcp.allowed_origins` gets 403 (DNS-rebinding defence).
  - An unsupported `MCP-Protocol-Version` header gets 400.
  - Tool-call timeouts use the SDK's own `read_timeout_seconds`, not task cancellation
    (ADR 0007).
  - A stdio server's environment is its own `env:` block plus the SDK's safe base (`PATH`,
    `HOME`, `USER`, ...). It does not inherit the gateway's environment, which holds provider
    keys. A policy whose server `env:` references a provider's `api_key_env` is refused at
    load. (Before this change every stdio server saw every provider key.)
- **Tracing:** each call is an `execute_tool <name>` span (GenAI conventions:
  `gen_ai.operation.name=execute_tool`, `gen_ai.tool.name`).

**Not done:**
- No rate limits or budgets on tool calls. The chat API's limits don't apply here.
- No resources, prompts, sampling or elicitation.
- Tool lists are cached for 60 s, and `listChanged` notifications are not relayed.
- The audit log has no retention policy.
- Upstream credentials for HTTP servers go in the URL or env only. There is no OAuth to
  upstream servers.
