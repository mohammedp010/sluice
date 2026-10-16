"""Text -> unit vector, via any OpenAI-compatible /embeddings endpoint (Ollama locally: $0)."""
from __future__ import annotations

from typing import Protocol

import httpx
import numpy as np


class Embedder(Protocol):
    name: str

    async def embed(self, texts: list[str]) -> np.ndarray: ...  # (n, dim), rows L2-normalised


def normalise(m: np.ndarray) -> np.ndarray:
    """Scale rows to length 1, so cosine similarity becomes a plain dot product."""
    m = np.asarray(m, dtype=np.float32)
    norms = np.linalg.norm(m, axis=1, keepdims=True)
    return m / np.maximum(norms, 1e-12)


class OpenAICompatEmbedder:
    def __init__(self, base_url: str, model: str, prefix: str = "", timeout_s: float = 5.0,
                 client: httpx.AsyncClient | None = None):
        self.name = model
        self.model = model
        # some models are trained with task prefixes (nomic: "clustering: ", "search_query: ")
        self.prefix = prefix
        self._client = client or httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout_s)

    async def embed(self, texts: list[str]) -> np.ndarray:
        r = await self._client.post("/embeddings", json={
            "model": self.model, "input": [self.prefix + t for t in texts]})
        r.raise_for_status()
        data = sorted(r.json()["data"], key=lambda d: d["index"])
        return normalise(np.array([d["embedding"] for d in data]))

    async def aclose(self) -> None:
        await self._client.aclose()
