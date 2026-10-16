"""Provider-neutral types and the interface every upstream adapter implements.

The gateway speaks the OpenAI wire format to clients, but internally it passes around
these small normalized types. Adapters translate between them and each upstream's format.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field


@dataclass
class Message:
    role: str
    content: str


@dataclass
class ChatParams:
    model: str  # the upstream model name (already resolved from the route)
    messages: list[Message]
    temperature: float | None = None
    max_tokens: int | None = None
    response_format: dict | None = None


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass
class Completion:
    content: str
    finish_reason: str = "stop"
    usage: Usage = field(default_factory=Usage)


@dataclass
class Chunk:
    """One streamed piece. `content` is a text delta; the last chunk carries finish_reason."""

    content: str = ""
    finish_reason: str | None = None
    usage: Usage | None = None


class ProviderError(Exception):
    """Upstream failed. `status` is the upstream HTTP status when there is one.

    `status` None means no HTTP answer at all (connect failure, timeout, stream cut).
    """

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status

    @property
    def retryable(self) -> bool:
        """Would the same request plausibly succeed later or elsewhere?

        Transport failures, timeouts, 408, 429 and 5xx: yes. Other 4xx (bad request, bad key,
        content policy): no -- retrying or failing over just repeats the same mistake.
        """
        s = self.status
        return s is None or s in (408, 429) or s >= 500

    @property
    def failover(self) -> bool:
        """Could a *different* provider succeed? Everything retryable, plus 401/403/404: this
        provider rejected our credentials or doesn't serve the model. That is the provider's
        (config) fault, not the client's, so the next target should get a chance."""
        return self.retryable or self.status in (401, 403, 404)


class Provider(ABC):
    name: str

    @abstractmethod
    async def complete(self, params: ChatParams) -> Completion: ...

    @abstractmethod
    def stream(self, params: ChatParams) -> AsyncIterator[Chunk]: ...

    async def aclose(self) -> None:
        return None
