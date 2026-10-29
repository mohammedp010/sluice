"""Policy file loading. Routes are data, not code (see policies/routes.yaml)."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator

from .providers.base import Provider
from .providers.mock import MockProvider
from .providers.openai_compat import OpenAICompatProvider


class ProviderConfig(BaseModel):
    type: Literal["mock", "openai_compat"]
    # mock options
    text: str | None = None
    latency_ms: float = 0
    tokens_per_sec: float | None = None
    error_rate: float = 0.0
    error_status: int = 500
    cut_stream_after: int | None = None
    # openai_compat options
    base_url: str | None = None
    api_key_env: str | None = None  # name of an env var; never put secrets in YAML
    timeout_s: float = 60.0
    # circuit breaker (health is a property of the provider, so it lives here)
    paid: bool = False  # real money: blocked unless SLUICE_ALLOW_PAID=1, and daily-capped
    breaker_failures: int = 3
    breaker_cooldown_s: float = 10.0

    @model_validator(mode="after")
    def _check(self):
        if self.type == "openai_compat" and not self.base_url:
            raise ValueError("openai_compat provider needs base_url")
        return self


class Target(BaseModel):
    provider: str
    upstream_model: str
    timeout_s: float = 30.0  # per attempt; for streams, time to first chunk
    retries: int = Field(default=1, ge=0)  # extra attempts on THIS target before failing over
    # USD per million tokens. Local models are really free; set fake prices to exercise budgets.
    price_in_per_mtok: float = 0.0
    price_out_per_mtok: float = 0.0


Check = Literal["empty", "truncated", "refusal", "invalid_json", "repetition"]


class Route(BaseModel):
    chain: list[Target] = Field(min_length=1)  # tried in order
    deadline_s: float = 60.0  # total budget across all attempts
    cache: bool = False  # semantic cache opt-in, per route (see CacheConfig)
    # Quality routing: if a non-streamed answer fails one of `escalate_on`, ask this (stronger)
    # route instead and bill both. Streams go straight to it: a sent answer can't be recalled.
    escalate: Route | None = None
    # Streams that die after their first chunk: "off" ends with an error event (ADR 0001);
    # "prefill" / "instruct" continue once on the next provider from the text already sent.
    stream_resume: Literal["off", "prefill", "instruct"] = "off"
    escalate_on: list[Check] = Field(
        default_factory=lambda: ["empty", "truncated", "refusal", "invalid_json", "repetition"])

    @model_validator(mode="before")
    @classmethod
    def _shorthand(cls, v):
        # allow `provider:` + `upstream_model:` for a single-target route
        if isinstance(v, dict) and "chain" not in v and "provider" in v:
            v = dict(v)
            keys = ("provider", "upstream_model", "timeout_s", "retries",
                    "price_in_per_mtok", "price_out_per_mtok")
            single = {k: v.pop(k) for k in keys if k in v}
            v["chain"] = [single]
        return v


class AuthConfig(BaseModel):
    enabled: bool = True
    db_path: str = "sluice.db"
    max_body_bytes: int = 1_000_000
    # Lifetime of keys made by `sluice.admin create-key` without --ttl, e.g. "90d". None: never
    # expire. (ADR 0011)
    key_ttl: str | None = None
    # Proxies whose X-Forwarded-For is believed when finding the caller's IP (key CIDR binding,
    # admin loopback check). Empty: the TCP peer is the caller.
    trusted_proxies: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self):
        from .auth.keys import parse_duration, parse_networks

        if self.key_ttl is not None:
            parse_duration(self.key_ttl)
        parse_networks(self.trusted_proxies)
        return self


class StateConfig(BaseModel):
    backend: Literal["memory", "redis"] = "memory"
    redis_url: str = "redis://localhost:6379/0"


class LimitsConfig(BaseModel):
    default_completion_estimate: int = 256  # assumed output size when max_tokens is not given
    # sent upstream when the client sets no limit, so a looping small model can't run forever
    default_max_tokens: int | None = 1024
    paid_daily_call_cap: int = 50


class TelemetryConfig(BaseModel):
    service_name: str = "sluice"
    traces: Literal["none", "console", "otlp"] = "none"
    otlp_endpoint: str = "http://localhost:4318"  # OTLP/HTTP; Jaeger listens here
    prometheus: bool = False  # serve /metrics
    ledger: bool = True  # one usage row per request in auth.db_path (cost attribution)


class CacheConfig(BaseModel):
    enabled: bool = False
    embed_base_url: str = "http://localhost:11434/v1"  # any OpenAI-compatible /embeddings
    embed_model: str = "nomic-embed-text"
    embed_prefix: str = "clustering: "  # task prefix nomic models are trained with
    # Cosine similarity needed for a hit, chosen together with the model, prefix and guard by
    # bench/cache_eval.py (see bench/results/cache-eval.md). Change one, re-run the eval.
    threshold: float = Field(default=0.895, gt=0, le=1)
    # veto hits between near-identical prompts (<= this many differing words); None = off
    guard_max_diff: int | None = 2
    ttl_s: float = 3600
    max_entries: int = 10_000
    max_temperature: float = 0.0  # requests asking for more randomness than this skip the cache


_ENV_REF = re.compile(r"\$(?:(\w+)|\{([^}]*)\})")  # $VAR or ${VAR}


class McpServerConfig(BaseModel):
    """One upstream MCP server: a local command (stdio) or a URL (Streamable HTTP)."""

    command: list[str] | None = None
    # The server's whole environment, on top of the SDK's safe base (PATH, HOME, ...): it does
    # not inherit the gateway's. Values may be "$VAR" (read at start), never a provider key.
    env: dict[str, str] = Field(default_factory=dict)
    url: str | None = None
    timeout_s: float = 30.0  # per tool call

    @model_validator(mode="after")
    def _one(self):
        if (self.command is None) == (self.url is None):
            raise ValueError("an MCP server needs exactly one of `command` or `url`")
        return self


class McpConfig(BaseModel):
    servers: dict[str, McpServerConfig] = Field(default_factory=dict)
    audit_db: str | None = None  # default: auth.db_path
    # Retention (ADR 0012): rows older than this are deleted hourly and checkpointed, so the
    # hash chain still verifies. None: keep everything.
    audit_retention_days: float | None = Field(default=None, gt=0)
    # Browsers send Origin; MCP's spec requires checking it (DNS rebinding). CLI clients don't
    # send one. Any Origin not listed here is refused.
    allowed_origins: list[str] = Field(default_factory=list)
    # Tool pinning (ADR 0011). auto_first: trust a tool on first sight unless the lint flags
    # it; manual: every new tool waits for `sluice.admin tools approve`. A changed definition
    # is quarantined either way, found at the next refresh (every tools_ttl_s).
    pin_mode: Literal["auto_first", "manual"] = "auto_first"
    tools_ttl_s: float = Field(default=60.0, gt=0)
    lint_max_description: int = Field(default=1024, ge=1)


class GuardConfig(BaseModel):
    """Secret egress guard (ADR 0012). Tool rules map `server__tool` globs to a mode; the first
    glob that matches wins, and a tool no glob matches is `off`. Findings are logged as type and
    position, never the value."""

    # off | audit | block: block refuses the call (and C5 suspends the key)
    tool_args: dict[str, Literal["off", "audit", "block"]] = Field(
        default_factory=lambda: {"*": "audit"})
    # off | audit | redact: redact replaces each match with [REDACTED:<type>]
    tool_results: dict[str, Literal["off", "audit", "redact"]] = Field(
        default_factory=lambda: {"*": "audit"})
    # prompts sent upstream: off | audit | block (400 secret_in_prompt)
    chat: Literal["off", "audit", "block"] = "audit"

    @staticmethod
    def mode(rules: dict[str, str], tool: str) -> str:
        import fnmatch

        return next((m for g, m in rules.items() if fnmatch.fnmatchcase(tool, g)), "off")


class WatchConfig(BaseModel):
    """Behaviour watch (ADR 0012): small rules, counted in the shared state so every replica
    sees the same counts. Suspending is automatic and reversible (`sluice.admin unsuspend-key`);
    revoking stays human. With `enforce: false` every action becomes an alert."""

    enabled: bool = True
    enforce: bool = True
    # R1 probe: refused tool calls (denied, unknown, pending, quarantined) -> suspend the key
    probe_calls: int = Field(default=10, ge=1)
    probe_window_s: int = Field(default=300, ge=1)
    # R2 burn rate: this share of the daily budget spent within the window -> suspend. Keys
    # without a daily budget use `burn_usd` instead (None: not watched).
    burn_fraction: float = Field(default=0.5, gt=0, le=1)
    burn_usd: float | None = 10.0
    burn_window_s: int = Field(default=600, ge=1)
    # R3 new network: a key's first request from a /24 (IPv6: /64) unseen for this long -> alert
    new_network_days: int = Field(default=30, ge=1)
    # R4 scanner: this many failed key checks from one IP in the window, or one bearer value
    # that isn't key-shaped (quotes, spaces, SQL) -> refuse that IP for `block_ip_s`
    scanner_failures: int = Field(default=20, ge=1)
    scanner_window_s: int = Field(default=60, ge=1)
    block_ip_s: int = Field(default=900, ge=1)
    # Alerts also go to this webhook (Slack-compatible JSON), read from the env var named here
    webhook_env: str = "SLUICE_ALERT_WEBHOOK"
    recent_alerts: int = Field(default=200, ge=1)  # kept in memory for the admin API


class AdminConfig(BaseModel):
    """Read-only admin API under /v1/sluice/admin (policy, recent traces, ledger, keys without
    secrets, MCP audit) for the UI. Off by default: it shows who spent what. Loopback-only
    unless the env var `token_env` holds an admin token (32+ chars); then it needs that token."""

    enabled: bool = False
    token_env: str = "SLUICE_ADMIN_TOKEN"  # name of an env var; never put the token in YAML
    recent_traces: int = Field(default=200, ge=1)  # traces held in memory for the UI


class Settings(BaseModel):
    auth: AuthConfig = Field(default_factory=AuthConfig)
    telemetry: TelemetryConfig = Field(default_factory=TelemetryConfig)
    cache: CacheConfig = Field(default_factory=CacheConfig)
    mcp: McpConfig = Field(default_factory=McpConfig)
    state: StateConfig = Field(default_factory=StateConfig)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    admin: AdminConfig = Field(default_factory=AdminConfig)
    guard: GuardConfig = Field(default_factory=GuardConfig)
    watch: WatchConfig = Field(default_factory=WatchConfig)
    providers: dict[str, ProviderConfig]
    routes: dict[str, Route] = Field(default_factory=dict)  # public model name -> target

    @model_validator(mode="after")
    def _refs(self):
        for model, route in self.routes.items():
            r = route
            while r is not None:
                for t in r.chain:
                    if t.provider not in self.providers:
                        raise ValueError(
                            f"route '{model}' references unknown provider '{t.provider}'")
                r = r.escalate
        # A tool server is third-party code; it never needs an LLM key, so asking is an error
        provider_keys = {c.api_key_env for c in self.providers.values() if c.api_key_env}
        for server, m in self.mcp.servers.items():
            for var, value in m.env.items():
                refs = {a or b for a, b in _ENV_REF.findall(value)}  # as os.path.expandvars
                if refs & provider_keys:
                    raise ValueError(f"mcp server '{server}' env '{var}' references provider key "
                                     f"{sorted(refs & provider_keys)}; tool servers may not get one")
        return self


def load_settings(path: str | Path) -> Settings:
    return Settings.model_validate(yaml.safe_load(Path(path).read_text()))


def build_state(settings: Settings):
    from .limits.state import MemoryState, RedisState

    if settings.state.backend == "redis":
        return RedisState(settings.state.redis_url)
    return MemoryState()


def build_cache(settings: Settings):
    if not settings.cache.enabled:
        return None
    from .cache.embed import OpenAICompatEmbedder
    from .cache.semantic import SemanticCache
    from .cache.store import MemoryVectorStore

    c = settings.cache
    return SemanticCache(
        OpenAICompatEmbedder(c.embed_base_url, c.embed_model, c.embed_prefix),
        MemoryVectorStore(c.max_entries), c.threshold, c.ttl_s, c.max_temperature,
        c.guard_max_diff)


def build_breakers(settings: Settings) -> dict:
    from .router.breaker import CircuitBreaker

    return {
        n: CircuitBreaker(c.breaker_failures, c.breaker_cooldown_s)
        for n, c in settings.providers.items()
    }


def build_providers(settings: Settings) -> dict[str, Provider]:
    import os

    out: dict[str, Provider] = {}
    for name, c in settings.providers.items():
        if c.type == "mock":
            kw = c.model_dump(include={"latency_ms", "tokens_per_sec", "error_rate",
                                       "error_status", "cut_stream_after"})
            if c.text is not None:
                kw["text"] = c.text
            out[name] = MockProvider(name=name, **kw)
        else:
            key = os.environ.get(c.api_key_env) if c.api_key_env else None
            out[name] = OpenAICompatProvider(name, c.base_url, api_key=key, timeout_s=c.timeout_s)
    return out
