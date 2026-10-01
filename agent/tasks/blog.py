"""Draft the long-form blog post with the LLM, grounded in fetched source articles, with one repair attempt."""

from pydantic import BaseModel, Field

from agentkit import checks, llm, task_runner

TASK = "content.draft_post"
PROMPT_DIR = "agent/prompts"


class PostDraft(BaseModel):
    title: str = Field(min_length=5, max_length=140)
    body_markdown: str = Field(min_length=800, max_length=12000)
    tags: list[str] = Field(max_length=4)


def _make_validate(sources_text: str):
    def _validate(result: llm.Completion) -> list[str]:
        p = result.parsed
        if not isinstance(p, PostDraft):
            return []
        return checks.run_checks(
            checks.check_banned(p.body_markdown),
            checks.check_length(p.body_markdown, 800, 12000),
            checks.check_no_leaks(p.title + " " + p.body_markdown),
            checks.check_grounded(p.title + " " + p.body_markdown, sources_text)
            if sources_text
            else [],
        )

    return _validate


async def _once(topic: str, sources_text: str, feedback: str | None) -> tuple[PostDraft, list[str]]:
    completion, problems = await task_runner.run_task(
        TASK,
        topic,
        schema=PostDraft,
        prompt_dir=PROMPT_DIR,
        use_examples=False,
        use_knowledge=False,  # facts come from the fetched sources only
        validate=_make_validate(sources_text),
        feedback=feedback,
        temperature=0.3,
        max_tokens=3000,
        timeout=300.0,
    )
    parsed = completion.parsed
    if not isinstance(parsed, PostDraft):
        raise TypeError("draft_post returned no structured result")
    return parsed, problems


async def draft_post(topic: str, sources_text: str = "") -> tuple[PostDraft, list[str]]:
    """topic: the topic block including the numbered SOURCES. sources_text: the same source text, used to
    verify that technical identifiers were not invented. Failing checks trigger ONE rewrite with the exact
    problems listed; the better of the two drafts is kept. Anything still flagged goes to human review."""
    post, problems = await _once(topic, sources_text, None)
    if problems:
        fix = (
            "Your draft has these problems: "
            + "; ".join(problems[:8])
            + ". Rewrite the whole post and fix every one. "
            "Remove or replace anything not stated in the SOURCES, and do not mention the SOURCES themselves."
        )
        retry, retry_problems = await _once(topic, sources_text, fix)
        if len(retry_problems) <= len(problems):
            post, problems = retry, retry_problems
    return post, problems
