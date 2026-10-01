"""Draft a technical blog post with the LLM."""

from pydantic import BaseModel, Field

from agentkit import checks, llm, task_runner

TASK = "content.draft_post"
PROMPT_DIR = "services/content-service/app/prompts"


class PostDraft(BaseModel):
    title: str = Field(min_length=5, max_length=140)
    body_markdown: str = Field(min_length=800, max_length=12000)
    tags: list[str] = Field(default_factory=list, max_length=4)


def _validate(result: llm.Completion) -> list[str]:
    p = result.parsed
    if not isinstance(p, PostDraft):
        return []
    return checks.run_checks(
        checks.check_banned(p.body_markdown),
        checks.check_length(p.body_markdown, 800, 12000),
    )


async def draft_post(topic: str) -> tuple[PostDraft, list[str]]:
    """topic: title and angle. Every draft needs human technical review before publishing."""
    completion, problems = await task_runner.run_task(
        TASK,
        topic,
        schema=PostDraft,
        prompt_dir=PROMPT_DIR,
        use_knowledge=True,
        validate=_validate,
        temperature=0.6,
        max_tokens=3000,
        timeout=300.0,
    )
    parsed = completion.parsed
    if not isinstance(parsed, PostDraft):
        raise TypeError("draft_post returned no structured result")
    return parsed, problems
