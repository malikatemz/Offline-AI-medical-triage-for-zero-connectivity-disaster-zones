"""
RescueNet — Embedding utility
Replaces placeholder zero-vector in inference.py with real nomic-embed-text embeddings.
Used by: api/inference.py _fetch_rag_context() and core/ingest.py
"""

import asyncio
import hashlib
import os
from functools import lru_cache
from typing import Optional

import httpx

OLLAMA_URL  = os.getenv("OLLAMA_URL", "http://ollama:11434")
EMBED_MODEL = "nomic-embed-text"   # 274MB — pulled by model-init service
VECTOR_DIM  = 768                  # nomic-embed-text output dim


@lru_cache(maxsize=512)
def _cache_key(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest()


# In-memory embedding cache — avoids re-embedding same queries in a session
_embed_cache: dict[str, list[float]] = {}


async def embed(text: str, client: Optional[httpx.AsyncClient] = None) -> list[float]:
    """
    Embed text via Ollama nomic-embed-text.
    Falls back to zero vector if Ollama unavailable (offline degraded mode).
    """
    key = _cache_key(text)
    if key in _embed_cache:
        return _embed_cache[key]

    async def _call(c: httpx.AsyncClient) -> list[float]:
        r = await c.post(
            f"{OLLAMA_URL}/api/embeddings",
            json={"model": EMBED_MODEL, "prompt": text},
            timeout=5,
        )
        r.raise_for_status()
        return r.json()["embedding"]

    try:
        if client:
            vector = await _call(client)
        else:
            async with httpx.AsyncClient() as c:
                vector = await _call(c)
        _embed_cache[key] = vector
        return vector
    except Exception:
        # Degraded mode: zero vector → Qdrant returns low-confidence results
        # Logger will note "embedding_degraded: true" in record
        return [0.0] * VECTOR_DIM


async def embed_batch(texts: list[str]) -> list[list[float]]:
    """Embed multiple texts concurrently."""
    async with httpx.AsyncClient() as client:
        return await asyncio.gather(*[embed(t, client) for t in texts])
