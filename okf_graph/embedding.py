"""Embedder factory.

Primary path: OpenAI (text-embedding-3-small, 1536 dims) via neo4j-graphrag.
Offline path: a deterministic hash-based embedder so the full pipeline
(ingest -> vector index -> retrievers) can be smoke-tested and rehearsed
without any API key. Hash vectors carry no semantics — retrieval quality is
meaningless with them — but every moving part can be exercised.
"""

from __future__ import annotations

import hashlib
import math
import os
import re


DEFAULT_OPENAI_MODEL = "text-embedding-3-small"
OPENAI_DIMENSIONS = {"text-embedding-3-small": 1536, "text-embedding-3-large": 3072}


class HashEmbedder:
    """Deterministic bag-of-hashed-tokens embedding. Offline smoke tests only."""

    def __init__(self, dimensions: int = 256):
        self.dimensions = dimensions

    def embed_query(self, text: str) -> list[float]:
        vec = [0.0] * self.dimensions
        for token in re.findall(r"[a-z0-9]+", text.lower()):
            h = int.from_bytes(hashlib.blake2b(token.encode(), digest_size=8).digest(), "big")
            vec[h % self.dimensions] += 1.0 + (h >> 32) % 7 / 10.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]


def get_embedder(provider: str | None = None, model: str | None = None):
    """Return (embedder, dimensions). provider: 'openai' (default) or 'hash'."""
    provider = (provider or os.getenv("EMBEDDING_PROVIDER", "openai")).lower()
    if provider not in ("openai", "hash"):
        raise ValueError(f"unknown embedding provider {provider!r} — use 'openai' or 'hash'")
    if provider == "hash":
        emb = HashEmbedder()
        return emb, emb.dimensions
    from neo4j_graphrag.embeddings import OpenAIEmbeddings
    model = model or os.getenv("EMBEDDING_MODEL", DEFAULT_OPENAI_MODEL)
    return OpenAIEmbeddings(model=model), OPENAI_DIMENSIONS.get(model, 1536)
