"""In-memory vector store: exact nearest-neighbour search with numpy, written to see the maths.

One preallocated (capacity x dim) matrix used as a ring buffer, so the oldest entry is
overwritten when full (FIFO eviction, no rebuilds). Search is a single matrix-vector product:
rows and query are unit vectors, so `vecs @ q` is cosine similarity for every entry at once.
That's O(n * dim) per lookup, which is fine for ~10k entries (well under 1ms). Past that you need an
approximate index (HNSW / IVF, as pgvector offers), trading a little recall for sub-linear time.

Scopes (tenant + model + params) are compared as 64-bit hashes in the same vectorised pass, so
entries from another tenant can never be returned, even if their text is identical.
"""
from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import numpy as np


def scope_id(scope: str) -> int:
    return int.from_bytes(hashlib.sha256(scope.encode()).digest()[:8], "big", signed=True)


@dataclass
class Match:
    similarity: float
    value: Any


class MemoryVectorStore:
    def __init__(self, capacity: int = 10_000, clock: Callable[[], float] = time.time):
        self.capacity = capacity
        self._clock = clock
        self._vecs: np.ndarray | None = None  # allocated on first add, once dim is known
        self._scopes = np.zeros(capacity, dtype=np.int64)
        self._expires = np.zeros(capacity, dtype=np.float64)  # 0 = empty slot
        self._values: list[Any] = [None] * capacity
        self._next = 0  # ring-buffer write position

    def __len__(self) -> int:
        return int((self._expires > self._clock()).sum())

    def add(self, scope: str, vec: np.ndarray, value: Any, ttl_s: float) -> None:
        if self._vecs is None or self._vecs.shape[1] != vec.shape[0]:
            # first entry, or the embedding model changed: old vectors are meaningless now
            self._vecs = np.zeros((self.capacity, vec.shape[0]), dtype=np.float32)
            self._expires[:] = 0
        i = self._next
        self._vecs[i] = vec
        self._scopes[i] = scope_id(scope)
        self._expires[i] = self._clock() + ttl_s
        self._values[i] = value
        self._next = (i + 1) % self.capacity

    def search(self, scope: str, vec: np.ndarray) -> Match | None:
        """Best live entry in this scope, or None. The caller applies the threshold."""
        if self._vecs is None or self._vecs.shape[1] != vec.shape[0]:
            return None
        sims = self._vecs @ vec
        valid = (self._scopes == scope_id(scope)) & (self._expires > self._clock())
        if not valid.any():
            return None
        sims = np.where(valid, sims, -np.inf)
        i = int(np.argmax(sims))
        return Match(float(sims[i]), self._values[i])
