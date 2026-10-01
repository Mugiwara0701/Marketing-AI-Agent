"""Extract a public business contact from a company page with the LLM."""

from pydantic import BaseModel, Field

from agentkit import checks, llm, task_runner

TASK = "lead.extract_contact"
PROMPT_DIR = "agent/prompts"


class ContactResult(BaseModel):
    found: bool
    email: str = ""
    name: str = ""
    role: str = ""
    evidence: str = Field(default="", max_length=300)
    confidence: float = Field(ge=0, le=1)


def _validate(result: llm.Completion) -> list[str]:
    p = result.parsed
    if not isinstance(p, ContactResult):
        return []
    problems = checks.check_confidence(p.confidence)
    if p.found and "@" not in p.email:
        problems.append("found=true but no email")
    return problems


async def extract_contact(page_text: str) -> tuple[ContactResult, list[str]]:
    completion, problems = await task_runner.run_task(
        TASK,
        page_text,
        schema=ContactResult,
        prompt_dir=PROMPT_DIR,
        use_examples=False,
        validate=_validate,
        max_tokens=300,
    )
    parsed = completion.parsed
    if not isinstance(parsed, ContactResult):
        raise TypeError("extract_contact returned no structured result")
    return parsed, problems
