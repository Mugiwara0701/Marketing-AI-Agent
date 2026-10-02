"""Draft the single short follow-up to an intro email that got no open and no reply."""

from pydantic import BaseModel, Field

from agentkit import checks, llm, task_runner

from .proposal import _check_claims

TASK = "email.followup"
PROMPT_DIR = "agent/prompts"


class FollowUp(BaseModel):
    body: str = Field(min_length=30, max_length=900)


def _validate(result: llm.Completion) -> list[str]:
    p = result.parsed
    if not isinstance(p, FollowUp):
        return []
    return checks.run_checks(
        checks.check_banned(p.body),
        checks.check_length(p.body, 40, 800),
        checks.check_no_leaks(p.body),
        checks.check_known_names(p.body, set()),
        _check_claims(p.body),
    )


async def draft_followup(context: str) -> tuple[FollowUp, list[str]]:
    """context: company, project and the intro email we sent. Returns (draft, problems)."""
    completion, problems = await task_runner.run_task(
        TASK,
        context,
        schema=FollowUp,
        prompt_dir=PROMPT_DIR,
        use_examples=True,
        use_knowledge=True,
        validate=_validate,
        temperature=0.5,
        max_tokens=400,
    )
    parsed = completion.parsed
    if not isinstance(parsed, FollowUp):
        raise TypeError("draft_followup returned no structured result")
    return parsed, problems
