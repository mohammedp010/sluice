"""Policy file loading. Routes are data, not code (see policies/routes.yaml)."""
from __future__ import annotations

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


class Route(BaseModel):
    chain: list[Target] = Field(min_length=1)  # tried in order
    deadline_s: float = 60.0  # total budget across all attempts

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


class StateConfig(BaseModel):
    backend: Literal["memory", "redis"] = "memory"
    redis_url: str = "redis://localhost:6379/0"


class LimitsConfig(BaseModel):
    default_completion_estimate: int = 256  # assumed output size when max_tokens is not given
    # sent upstream when the client sets no limit, so a looping small model can't run forever
    default_max_tokens: int | None = 1024
    paid_daily_call_cap: int = 50


class Settings(BaseModel):
    auth: AuthConfig = Field(default_factory=AuthConfig)
    state: StateConfig = Field(default_factory=StateConfig)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    providers: dict[str, ProviderConfig]
    routes: dict[str, Route] = Field(default_factory=dict)  # public model name -> target

    @model_validator(mode="after")
    def _refs(self):
        for model, route in self.routes.items():
            for t in route.chain:
                if t.provider not in self.providers:
                    raise ValueError(f"route '{model}' references unknown provider '{t.provider}'")
        return self


def load_settings(path: str | Path) -> Settings:
    return Settings.model_validate(yaml.safe_load(Path(path).read_text()))


def build_state(settings: Settings):
    from .limits.state import MemoryState, RedisState

    if settings.state.backend == "redis":
        return RedisState(settings.state.redis_url)
    return MemoryState()


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
