"""Semantic cache policy: when to look, what counts as "the same question", when to store.

Only the *last user message* is compared by meaning. Everything else that changes the answer
must match exactly, and goes into the scope:
  tenant (team, else key)  -- never serve one tenant's answer to another
  public model             -- a cheaper model's answer isn't the stronger model's
  temperature, max_tokens  -- different sampling and length make a different answer
  every earlier message    -- includes the system prompt; same question in another conversation
                              isn't the same question
A cache is only worth having if its false hits are rare: a miss costs one upstream call, a
false hit confidently returns the answer to a different question. So the threshold is chosen
from a labelled eval (bench/cache_eval.py), not by feel.

Embeddings encode topic, so they are nearly blind to the edits that flip an answer while keeping
the topic: "5 km to miles" vs "5 miles to km" scores 0.99. The lexical guard vetoes a hit when the
two prompts are a *minimal pair*: the same words reordered, or word sets differing by only a few
words. It can only turn hits into misses, never create a hit, so a wrong guard costs hit rate and
never correctness.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field

import numpy as np

from ..providers.base import Message, Usage
from .embed import Embedder
from .store import Match, MemoryVectorStore


@dataclass
class CachedAnswer:
    prompt: str  # the user message that produced it (for the lexical guard)
    content: str
    finish_reason: str
    usage: Usage  # what the original upstream call used; a hit itself costs nothing
    created: float = field(default_factory=time.time)


@dataclass
class Probe:
    result: str  # hit | miss | skip
    reason: str | None = None  # why skipped
    scope: str = ""
    vec: np.ndarray | None = None
    match: Match | None = None
    may_store: bool = False

    @property
    def answer(self) -> CachedAnswer | None:
        return self.match.value if self.result == "hit" and self.match else None


def token_diff(a: str, b: str) -> int:
    """-1: identical after lower-casing and dropping punctuation (a plain repeat).
    0: same words in a different order ("km to miles" / "miles to km").
    n: number of words in one prompt's word set but not the other's."""
    ta, tb = re.findall(r"[a-z0-9%]+", a.lower()), re.findall(r"[a-z0-9%]+", b.lower())
    if ta == tb:
        return -1
    if sorted(ta) == sorted(tb):
        return 0
    return len(set(ta) ^ set(tb))


class SemanticCache:
    def __init__(self, embedder: Embedder, store: MemoryVectorStore, threshold: float,
                 ttl_s: float = 3600, max_temperature: float = 0.0,
                 guard_max_diff: int | None = 2):
        self.embedder = embedder
        self.store = store
        self.threshold = threshold
        self.ttl_s = ttl_s
        self.max_temperature = max_temperature
        self.guard_max_diff = guard_max_diff  # None disables the lexical guard

    @staticmethod
    def scope(tenant: str, model: str, temperature: float | None, max_tokens: int | None,
              history: list[Message]) -> str:
        raw = json.dumps([tenant, model, temperature, max_tokens,
                          [[m.role, m.content] for m in history]])
        return hashlib.sha256(raw.encode()).hexdigest()

    async def probe(self, *, tenant: str, model: str, messages: list[Message],
                    temperature: float | None, max_tokens: int | None,
                    cache_control: str = "") -> Probe:
        cc = {d.strip().lower() for d in cache_control.split(",")}
        may_read, may_store = "no-cache" not in cc, "no-store" not in cc
        if not (may_read or may_store):
            return Probe("skip", "cache_control")
        if temperature is not None and temperature > self.max_temperature:
            return Probe("skip", "temperature")  # the client asked for varied answers
        if messages[-1].role != "user":
            return Probe("skip", "not_user_turn")
        scope = self.scope(tenant, model, temperature, max_tokens, messages[:-1])
        try:
            vec = (await self.embedder.embed([messages[-1].content]))[0]
        except Exception:  # noqa: BLE001 -- the cache is an optimisation: fail open, never fail the request
            return Probe("skip", "embed_error")
        match = self.store.search(scope, vec) if may_read else None
        hit = match is not None and match.similarity >= self.threshold
        reason = None if may_read else "cache_control"
        if hit and self.guard_max_diff is not None and (
                0 <= token_diff(messages[-1].content, match.value.prompt) <= self.guard_max_diff):
            hit, reason = False, "lexical_guard"
        return Probe("hit" if hit else "miss", reason, scope, vec, match, may_store)

    def put(self, probe: Probe, answer: CachedAnswer) -> bool:
        """Store after a successful upstream answer. Only from a miss that allows storing."""
        if probe.result != "miss" or not probe.may_store or probe.vec is None:
            return False
        if not answer.content:
            return False  # never cache an empty answer
        self.store.add(probe.scope, probe.vec, answer, self.ttl_s)
        return True
