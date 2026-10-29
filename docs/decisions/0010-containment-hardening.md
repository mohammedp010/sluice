# ADR 0010: Containment, part 1: secrets out of reach, no unguarded doors, pinned supply

**Status:** accepted (Phase 9). Code: `src/sluice/mcp/upstream.py`, `src/sluice/api/admin.py`,
`src/sluice/api/app.py`, `src/sluice/config.py`, `Dockerfile`, `requirements*.lock`,
`.github/workflows/ci.yml`. Tests: `tests/test_invariants.py`, `tests/test_admin.py`,
`tests/test_mcp.py`.

**Context:** in 2026 AI gateways became a target in their own right, because one holds every
provider key.
- LiteLLM had a SQL injection in its key check (CVE-2026-42208), probed 36 h after the advisory.
- An MCP "test connection" endpoint ran whatever command it was sent (CVE-2026-42271, in CISA
  KEV).
- Read-only users could reach admin functions (CVE-2026-35029), and a request field redirected
  upstream calls, provider key included (CVE-2026-59823).
- A stolen maintainer token put malicious LiteLLM releases on PyPI for ~40 minutes.
- In the same months, npm worms (Shai-Hulud, keyv) stole tokens from developer machines and CI.
- Railway's October breach went after customer env vars to harvest AI provider keys.

This part assumes something will get in. It keeps what a compromise can reach small, and makes
the usual doors fail loudly in tests.

**Decision:**
- **Tool servers don't see the gateway's secrets.** A stdio MCP server gets its own `env:` block
  plus the SDK's safe base (`PATH`, `HOME`, ...), not `os.environ`. A policy that hands one a
  provider's `api_key_env` is refused at load (ADR 0009).
- **The admin API is a separate credential:**
  - With no token set, only loopback clients get in.
  - With `SLUICE_ADMIN_TOKEN` (the env var named by `admin.token_env`) set, every request needs
    it as a bearer token. It is compared in constant time, and anything under 32 characters is
    refused at startup.
  - A virtual key never opens the admin API, so a leaked app key can't read the ledger or the
    key list.
  - The UI's dev server adds the token server side (`UI/vite.config.ts`), so it never reaches
    the browser.
- **No map for strangers:**
  - `/docs`, `/redoc` and `/openapi.json` are off.
  - `/v1/models` needs a key and lists only the models that key may use. Unknown and forbidden
    look the same, as with MCP tools.
- **Every route is classified.** `tests/test_invariants.py` lists each route as public, key,
  admin or refused, and checks each one answers that way to a caller with no credential from a
  non-loopback address. A new route that isn't in the list fails the suite, so a forgotten
  "test" endpoint can't ship quietly.
- **No request field steers the upstream.** A test asserts the chat request schema has no
  `api_base`, `base_url`, `api_key`, `command` or similar field, and that unknown fields are
  dropped.
- **No SQL built from strings.** Ruff `S608` is on. Every query binds its values; the three that
  join constant column names carry a `noqa`.
- **Pinned supply:**
  - `requirements.lock` (runtime) and `requirements-dev.lock` pin every package by hash. They
    are generated in `python:3.12-slim`, the image the Dockerfile uses.
  - The Docker image installs only the runtime lock (`--require-hashes --no-deps`) and runs from
    source, so no build step fetches anything unverified.
  - The base image is pinned by digest, and `.dockerignore` is an allowlist, so `.env` files and
    `sluice.db` never reach the build context.
  - CI installs the dev lock, runs ruff, the tests (Redis included) and `pip-audit` on both
    locks. Its actions are pinned by commit SHA, and the checkout keeps no credentials.
  - `starlette>=1.0.1` is a direct floor (CVE-2026-48710, Host header auth bypass).

**Measured** (all on one MacBook, plus a clean `python:3.12-slim` container replaying the CI
job):
- Before the env fix, a stdio test server read the provider key. After it, the provider key
  and every other gateway variable are unset, and the configured `TOKEN` and `PATH` are present.
- Admin API against the real container, from the host (not loopback): 403 with no token set.
  With a token set: 401 with no header, 401 with a valid virtual key, 200 with the token. A
  7-character token stops the container at startup.
- The image answers 404 on `/docs` and `/openapi.json`, 401 on `/v1/models` without a key, and
  200 with one. The compose pair still enforces a shared 5 RPM limit (5 allowed across both
  replicas, then 429).
- 157 tests pass with Redis. `pip-audit` found no known vulnerabilities in either lock
  (29 Oct 2026).

**Trade-offs:**
- `/v1/sluice/health` (breaker state per provider) and `/metrics` stay unauthenticated, for
  load balancers, Prometheus and the UI. They name providers, but expose no keys, spend or
  traffic content.
- Loopback-only admin trusts anything on the same host. Behind a reverse proxy on that host,
  set a token. Uvicorn takes the client address from `X-Forwarded-For` only when the connection
  comes from 127.0.0.1, so remote callers behind a local proxy are still seen as remote.
- A pinned base image doesn't get OS security patches until the digest is bumped. That is the
  price of knowing exactly what runs.
- The locks are resolved for Linux and Python 3.12. Local development on other versions still
  uses `pip install -e '.[dev]'`.
- Regenerating them:

  ```
  docker run --rm -v "$PWD":/src -w /src python:3.12-slim sh -c "pip install pip-tools && pip-compile --generate-hashes --allow-unsafe --strip-extras --no-emit-index-url -o requirements.lock pyproject.toml && pip-compile --generate-hashes --allow-unsafe --strip-extras --no-emit-index-url --extra dev -o requirements-dev.lock pyproject.toml"
  ```

**Not done (later parts):**
- MCP tool definitions are not pinned, so a server can still change a tool's description after
  approval.
- Virtual keys don't expire and aren't bound to a network. There's no rotation command and no
  bulk revoke.
- Nothing watches key behaviour (burn rate, probing, new networks) or suspends a key
  automatically.
- No rate limits on tool calls (ADR 0009).
- Tool arguments and results are not scanned for secrets.
- The audit log has no retention and no tamper evidence.
- The container runs as root. A non-root user needs `./data` to be writable by that user on
  Linux hosts.
