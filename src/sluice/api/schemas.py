"""OpenAI chat-completions wire format (the subset Phase 1 supports)."""
from __future__ import annotations

from pydantic import BaseModel, Field


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatCompletionRequest(BaseModel):
    model: str
    messages: list[ChatMessage] = Field(min_length=1)
    stream: bool = False
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_tokens: int | None = Field(default=None, ge=1)
    # unknown fields (top_p, tools, ...) are accepted and ignored for now
    model_config = {"extra": "ignore"}
