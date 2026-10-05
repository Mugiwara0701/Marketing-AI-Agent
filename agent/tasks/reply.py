"""Classify an inbound reply to our outreach and, when it deserves one, draft the answer."""

from typing import Literal, get_args

from pydantic import BaseModel, Field

from agentkit import checks, llm, task_runner

from .proposal import _check_claims

TASK = "email.reply"
PROMPT_DIR = "agent/prompts"

Label = Literal["interested", "question", "not_interested", "ooo", "bounce", "unsubscribe"]
LABELS = set(get_args(Label))
NEEDS_REPLY = {"interested", "question"}


class ReplyResult(BaseModel):
    label: Label
    confidence: float = Field(ge=0, le=1)
    reply_body: str = Field(default="", max_length=1500)


def _validate(result: llm.Completion) -> list[str]:
    p = result.parsed
    if not isinstance(p, ReplyResult):
        return []
    problems = checks.run_checks(
        checks.check_label(p.label, LABELS), checks.check_confidence(p.confidence)
    )
    if p.label in NEEDS_REPLY:
        problems += checks.run_checks(
            checks.check_length(p.reply_body, 40, 1200),
            checks.check_banned(p.reply_body),
            checks.check_no_leaks(p.reply_body),
            checks.check_known_names(p.reply_body, set()),
            _check_claims(p.reply_body),
        )
    return problems


async def classify_and_draft(context: str) -> tuple[ReplyResult, list[str]]:
    """context: our sent mail and their reply. Returns (result, problems); problems mean human review."""
    completion, problems = await task_runner.run_task(
        TASK,
        context,
        schema=ReplyResult,
        prompt_dir=PROMPT_DIR,
        use_examples=True,
        use_knowledge=True,
        validate=_validate,
        temperature=0.3,
        max_tokens=600,
    )
    parsed = completion.parsed
    if not isinstance(parsed, ReplyResult):
        raise TypeError("classify_and_draft returned no structured result")
    return parsed, problems
