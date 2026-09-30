"""Run one model task end to end: prompt -> retrieval -> llm -> validation -> retrieval log."""

from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

from . import db, llm, prompts, retrieve
from .log import get_logger

log = get_logger("agentkit.task_runner")

T = TypeVar("T", bound=BaseModel)


async def run_task(
    task: str,
    untrusted: str,
    *,
    schema: type[T] | None = None,
    prompt_dir: str | Path | None = None,
    use_examples: bool = True,
    use_knowledge: bool = False,
    validate: Callable[[llm.Completion], list[str]] | None = None,
    **llm_kwargs,
) -> tuple[llm.Completion, list[str]]:
    """Returns (completion, problems). Empty problems = passed checks; otherwise mark needs_review."""
    template, version = await prompts.load(task, prompt_dir)
    examples = await retrieve.similar_examples(task, untrusted) if use_examples else []
    chunks = await retrieve.knowledge(untrusted) if use_knowledge else []
    msgs = prompts.build_messages(template, untrusted=untrusted, examples=examples, context=chunks)
    result = await llm.complete(task, msgs, schema, **llm_kwargs)
    problems = validate(result) if validate else []
    try:
        await db.log_retrieval(
            task,
            version,
            [e["id"] for e in examples],
            [c["id"] for c in chunks],
            "ok" if not problems else "needs_review",
        )
    except Exception:
        log.warning("retrieval log write failed", extra={"ctx": {"task": task}})
    return result, problems
