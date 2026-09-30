"""Retrieval over few-shot examples and knowledge chunks (exact cosine search; add HNSW only if needed)."""

from . import db
from .embed import embed_one


async def similar_examples(task: str, query: str, k: int = 4) -> list[dict]:
    """Nearest labelled examples for a task: [{id, input_text, output_json}]."""
    v = db.vec(await embed_one(query))
    rows = await db.fetch(
        """select id, input_text, output_json from examples
            where task = $1 and embedding is not null
            order by embedding <=> $2::vector limit $3""",
        task, v, k,
    )
    return [dict(r) for r in rows]


async def knowledge(query: str, k: int = 4) -> list[dict]:
    """Nearest knowledge chunks: [{id, doc_id, content, title, url}]."""
    v = db.vec(await embed_one(query))
    rows = await db.fetch(
        """select c.id, c.doc_id, c.content, d.title, d.url
             from knowledge_chunks c join knowledge_docs d on d.id = c.doc_id
            where c.embedding is not null
            order by c.embedding <=> $1::vector limit $2""",
        v, k,
    )
    return [dict(r) for r in rows]


async def nearest_distance(table: str, query: str) -> float | None:
    """Cosine distance to the closest stored item (duplicate check for posts/topics)."""
    if table not in {"content_posts", "topics"}:
        raise ValueError("unsupported table")
    v = db.vec(await embed_one(query))
    row = await db.fetchrow(
        f"select embedding <=> $1::vector as d from {table} where embedding is not null order by d limit 1", v
    )
    return None if row is None else float(row["d"])
