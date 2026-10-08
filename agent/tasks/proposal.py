"""Draft a company-specific proposal / outreach email with the LLM."""

from pydantic import BaseModel, Field

from agentkit import checks, llm, task_runner

TASK = "outreach.draft"
PROMPT_DIR = "agent/prompts"


class EmailDraft(BaseModel):
    subject: str = Field(min_length=3, max_length=120)
    body: str = Field(min_length=40, max_length=4500)


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


PITCH_TASK = "outreach.pitch"


class PitchParts(BaseModel):
    """A proposal email as named parts: the structure is guaranteed by code, the wording comes from the model."""

    subject: str = Field(min_length=3, max_length=120)
    greeting: str = Field(min_length=3, max_length=60)
    intro: str = Field(min_length=40, max_length=700)
    understanding: str = Field(min_length=40, max_length=800)
    offer_intro: str = Field(min_length=10, max_length=300)
    deliverables: list[str] = Field(min_length=3, max_length=4)
    engagement: str = Field(min_length=40, max_length=700)
    closing_question: str = Field(min_length=15, max_length=400)


def assemble(p: PitchParts) -> EmailDraft:
    """Greeting, intro, understanding, offer and bullets, engagement, closing question: always in this order. The fixed
    services paragraph is added later (outreach.with_services) just before the closing question."""
    bullets = "\n".join(f"- {d.strip().lstrip('-* ').strip()}" for d in p.deliverables)
    body = "\n\n".join([p.greeting.strip(), p.intro.strip(), p.understanding.strip(),
                         f"{p.offer_intro.strip()}\n{bullets}", p.engagement.strip(), p.closing_question.strip()])  # fmt: skip
    return EmailDraft(subject=p.subject.strip(), body=body)


def _validate_pitch(result: llm.Completion) -> list[str]:
    p = result.parsed
    if not isinstance(p, PitchParts):
        return []
    d = assemble(p)
    return checks.run_checks(
        checks.check_banned(d.subject + " " + d.body),
        checks.check_length(d.body, 900, 4000),
        checks.check_known_names(d.body, set()),
        _check_claims(d.subject + " " + d.body),
    )


async def draft_pitch(lead_context: str, company: str = "") -> tuple[EmailDraft, list[str]]:
    """A longer proposal email (see prompts/outreach_pitch.txt). `company` is the target's name: when the intro does
    not contain it, the model is asked once more with that feedback. Returns (draft, problems)."""
    feedback = None
    for attempt in range(2):
        completion, problems = await task_runner.run_task(
            PITCH_TASK,
            lead_context,
            schema=PitchParts,
            prompt_dir=PROMPT_DIR,
            use_examples=True,
            use_knowledge=True,
            validate=_validate_pitch,
            feedback=feedback,
            temperature=0.5,
            max_tokens=1600,
            reasoning_effort="none",
        )
        parts = completion.parsed
        if not isinstance(parts, PitchParts):
            raise TypeError("draft_pitch returned no structured result")
        if not company or company.lower() in parts.intro.lower() or attempt == 1:
            return assemble(parts), problems
        feedback = f'The "intro" must contain the company name "{company}" exactly. Write the JSON again with that fixed.'
    raise AssertionError("unreachable")
