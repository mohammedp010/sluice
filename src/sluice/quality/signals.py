"""Cheap checks on an answer that might mean "ask the stronger model instead".

These read only the answer itself (no extra model call), so they cost microseconds. That is
also their limit: they catch answers that are *visibly* broken (empty, cut off, refusing,
looping, unparseable JSON), not answers that are fluent and wrong. bench/routing_eval.py
measures how much of the cheap model's wrongness that actually is.
"""
from __future__ import annotations

import json
import re
from collections import Counter

REFUSAL = re.compile(
    r"\b(i can(?:no|')t (?:help|assist|answer|provide)|i'?m (?:sorry|unable)|i am (?:sorry|unable)"
    r"|as an ai\b|i (?:don't|do not) have (?:access|the ability))", re.IGNORECASE)
FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)


def parse_json(text: str):
    """The JSON value in `text`, tolerating a ```json fence and surrounding prose; None if none."""
    m = FENCE.match(text)
    text = m.group(1) if m else text
    try:
        return json.loads(text)
    except ValueError:
        pass
    dec = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch in "{[":
            try:
                return dec.raw_decode(text, i)[0]
            except ValueError:
                continue
    return None


def repetitive(text: str, n: int = 4, max_repeats: int = 3) -> bool:
    """Small models fall into loops ("the the the", the same sentence over and over)."""
    words = text.lower().split()
    if len(words) < 20:
        return False
    grams = Counter(tuple(words[i:i + n]) for i in range(len(words) - n + 1))
    return grams.most_common(1)[0][1] > max_repeats


def heuristic_flags(text: str, finish_reason: str | None = None,
                    expects_json: bool = False) -> list[str]:
    """Reasons to distrust an answer, cheapest first. Empty list: nothing visibly wrong."""
    t = text.strip()
    flags = []
    if not t:
        flags.append("empty")
    if finish_reason == "length":
        flags.append("truncated")
    if REFUSAL.search(t[:300]):
        flags.append("refusal")
    if expects_json and parse_json(t) is None:
        flags.append("invalid_json")
    if repetitive(t):
        flags.append("repetition")
    return flags
