"""Draft a first-touch outreach email with the LLM."""

from pydantic import BaseModel, Field

from agentkit import checks, llm, task_runner

TASK = "outreach.draft"
PROMPT_DIR = "services/outreach-service/app/prompts"


class EmailDraft(BaseModel):
    subject: str = Field(min_length=3, max_length=120)
    body: str = Field(min_length=40, max_length=1500)


def _validate(result: llm.Completion) -> list[str]:
    p = result.parsed
    if not isinstance(p, EmailDraft):
        return []
    return checks.run_checks(
        checks.check_banned(p.subject + " " + p.body),
        checks.check_length(p.body, 40, 1500),
        checks.check_known_names(p.body, set()),
    )


async def draft_email(lead_context: str) -> tuple[EmailDraft, list[str]]:
    """lead_context: company, signal (e.g. the job post) and contact role. Returns (draft, problems)."""
    completion, problems = await task_runner.run_task(
        TASK,
        lead_context,
        schema=EmailDraft,
        prompt_dir=PROMPT_DIR,
        use_examples=True,
        use_knowledge=True,
        validate=_validate,
        temperature=0.5,
        max_tokens=600,
    )
    parsed = completion.parsed
    if not isinstance(parsed, EmailDraft):
        raise TypeError("draft_email returned no structured result")
    return parsed, problems
