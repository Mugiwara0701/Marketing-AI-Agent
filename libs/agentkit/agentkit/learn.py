"""Learning loop: store human-approved outputs as few-shot examples, with their embedding."""

from . import db
from .embed import MODEL, embed_one


async def add_example(
    task: str,
    input_text: str,
    output_json: dict,
    label: str | None = None,
    source: str = "approved",
) -> None:
    v = db.vec(await embed_one(input_text))
    await db.execute(
        """insert into examples (task, input_text, output_json, label, source, embedding, embedding_model)
           values ($1,$2,$3::jsonb,$4,$5,$6::vector,$7)""",
        task,
        input_text,
        db.dumps(output_json),
        label,
        source,
        v,
        MODEL,
    )
