"""Token estimation before the call; reconciled with provider-reported usage afterwards.

Real tokenizers (tiktoken) are model-specific and need downloads. A chars/4 heuristic is within
tens of percent for English, and reconciliation corrects the bucket anyway.
"""
from __future__ import annotations

from ..providers.base import Message


def estimate_prompt_tokens(messages: list[Message]) -> int:
    return sum(len(m.content) // 4 + 4 for m in messages) + 3  # per-message overhead + priming


def cost_micro_usd(prompt: int, completion: int, price_in: float, price_out: float) -> int:
    """Prices are USD per million tokens, so tokens * price == micro-USD exactly."""
    return round(prompt * price_in + completion * price_out)
