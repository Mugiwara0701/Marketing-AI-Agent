"""Embeddings via the OpenAI-compatible /v1/embeddings route (verify the TEI route before relying on it)."""

import httpx

from .config import env

DIMENSIONS = 1024  # Qwen3-Embedding-0.6B; changing the model means a new column and re-embedding
MODEL = "Qwen/Qwen3-Embedding-0.6B"


async def embed(texts: list[str]) -> list[list[float]]:
    if not texts:
        return []
    base = env("EMBED_BASE_URL") or env("LLM_BASE_URL", required=True)
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post(
            f"{base.rstrip('/')}/v1/embeddings",
            json={"model": env("EMBED_MODEL", MODEL), "input": texts},
            headers={"Authorization": f"Bearer {env('LLM_API_KEY', '')}"},
        )
    r.raise_for_status()
    vectors = [d["embedding"] for d in sorted(r.json()["data"], key=lambda d: d["index"])]
    for v in vectors:
        if len(v) != DIMENSIONS:
            raise ValueError(f"embedding dimension {len(v)} != {DIMENSIONS}")
    return vectors


async def embed_one(text: str) -> list[float]:
    return (await embed([text]))[0]
