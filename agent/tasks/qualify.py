"""Qualify a public listing (job/project post, company page) as a services lead and extract its facts."""

from typing import Literal

from pydantic import BaseModel, Field

from agentkit import checks, llm, task_runner

TASK = "lead.qualify"
PROMPT_DIR = "agent/prompts"
SERVICES = Literal["AOSP", "BSP", "Automotive", "Embedded Linux", "Android HAL", "Other"]
THRESHOLD = 0.6


class QualifyResult(BaseModel):
    relevant: bool
    service_fit: list[SERVICES] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)
    reason: str = Field(max_length=400)
    # Required (no defaults) so schema-constrained decoding makes the model fill them in.
    company_name: str = Field(max_length=120)
    project_summary: str = Field(max_length=600)
    technologies: list[str] = Field(max_length=12)
    location: str = Field(max_length=120)
    website: str = Field(max_length=200)


def _validate(result: llm.Completion) -> list[str]:
    p = result.parsed
    return checks.check_confidence(p.confidence, THRESHOLD) if isinstance(p, QualifyResult) else []


async def qualify_signal(text: str) -> tuple[QualifyResult, list[str]]:
    """Returns (result, problems). Non-empty problems mean low confidence: route to review, not auto-drop."""
    completion, problems = await task_runner.run_task(
        TASK,
        text,
        schema=QualifyResult,
        prompt_dir=PROMPT_DIR,
        use_examples=False,
        validate=_validate,
        max_tokens=300,
        reasoning_effort="none",  # a thinking model otherwise spends the whole budget before the JSON
    )
    parsed = completion.parsed
    if not isinstance(parsed, QualifyResult):
        raise TypeError("qualify returned no structured result")
    return parsed, problems
