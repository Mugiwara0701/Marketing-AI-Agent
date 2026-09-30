"""Classify inbound replies and draft answers with the LLM."""

from typing import Literal

from pydantic import BaseModel, Field

from agentkit import checks, llm, task_runner

CLASSIFY_TASK = "outreach.classify_reply"
DRAFT_TASK = "outreach.draft_reply"
PROMPT_DIR = "services/outreach-service/app/prompts"
LABELS = (
    "interested",
    "question",
    "not_interested",
    "unsubscribe",
    "out_of_office",
    "bounce",
    "other",
)


class ReplyClass(BaseModel):
    label: Literal[
        "interested",
        "question",
        "not_interested",
        "unsubscribe",
        "out_of_office",
        "bounce",
        "other",
    ]
    confidence: float = Field(ge=0, le=1)
    reason: str = Field(max_length=300)


class ReplyDraft(BaseModel):
    body: str = Field(min_length=20, max_length=1200)


def _validate_class(result: llm.Completion) -> list[str]:
    p = result.parsed
    if not isinstance(p, ReplyClass):
        return []
    return checks.check_label(p.label, set(LABELS)) + checks.check_confidence(p.confidence)


def _validate_draft(result: llm.Completion) -> list[str]:
    p = result.parsed
    if not isinstance(p, ReplyDraft):
        return []
    return checks.run_checks(checks.check_banned(p.body), checks.check_known_names(p.body, set()))


async def classify_reply(reply_text: str) -> tuple[ReplyClass, list[str]]:
    completion, problems = await task_runner.run_task(
        CLASSIFY_TASK,
        reply_text,
        schema=ReplyClass,
        prompt_dir=PROMPT_DIR,
        validate=_validate_class,
        temperature=0.0,
        max_tokens=200,
    )
    parsed = completion.parsed
    if not isinstance(parsed, ReplyClass):
        raise TypeError("classify_reply returned no structured result")
    return parsed, problems


async def draft_reply(thread_text: str) -> tuple[ReplyDraft, list[str]]:
    completion, problems = await task_runner.run_task(
        DRAFT_TASK,
        thread_text,
        schema=ReplyDraft,
        prompt_dir=PROMPT_DIR,
        use_knowledge=True,
        validate=_validate_draft,
        temperature=0.4,
        max_tokens=500,
    )
    parsed = completion.parsed
    if not isinstance(parsed, ReplyDraft):
        raise TypeError("draft_reply returned no structured result")
    return parsed, problems
