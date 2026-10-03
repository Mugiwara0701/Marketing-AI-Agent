"""Draft a company-specific proposal / outreach email with the LLM."""

from pydantic import BaseModel, Field

from agentkit import checks, llm, task_runner

TASK = "outreach.draft"
PROMPT_DIR = "agent/prompts"


class EmailDraft(BaseModel):
    subject: str = Field(min_length=3, max_length=120)
    body: str = Field(min_length=40, max_length=2000)


# Claims about our past work are only allowed when "Reference material" backs them; we cannot verify
# that here, so any such phrase sends the draft to human review.
_UNBACKED_CLAIMS = (
    "we've delivered", "we have delivered", "we've built", "we have built", "we've shipped",
    "our experience", "our clients", "our customers", "years of experience", "we've helped",
    "we have helped", "we've worked", "we have worked", "similar projects", "track record",
)  # fmt: skip


def _check_claims(text: str) -> list[str]:
    low = text.lower().replace("\u2019", "'")
    return [f"unbacked claim about past work: {c!r}" for c in _UNBACKED_CLAIMS if c in low]


def _validate(result: llm.Completion) -> list[str]:
    p = result.parsed
    if not isinstance(p, EmailDraft):
        return []
    return checks.run_checks(
        checks.check_banned(p.subject + " " + p.body),
        checks.check_length(p.body, 80, 2000),
        checks.check_known_names(p.body, set()),
        _check_claims(p.subject + " " + p.body),
    )


async def draft_proposal(lead_context: str) -> tuple[EmailDraft, list[str]]:
    """lead_context: company, project summary, technologies, location, source and contact role. Returns (draft, problems)."""
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
        reasoning_effort="none",  # a thinking model otherwise spends the whole budget before the JSON
    )
    parsed = completion.parsed
    if not isinstance(parsed, EmailDraft):
        raise TypeError("draft_proposal returned no structured result")
    return parsed, problems
