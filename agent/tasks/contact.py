"""LLM task lead.extract_contact: the people and business addresses a company publishes on one of its own pages.

The model only reads. Code keeps a person only if the name and the address are literally on the page, the address
is on the company's own domain, and the role is one we may contact (agent.leadgen.contacts)."""

from typing import Annotated

from pydantic import BaseModel, BeforeValidator, Field

from agentkit import task_runner

TASK = "lead.extract_contact"
PROMPT_DIR = "agent/prompts"


def _clip(n: int):
    return BeforeValidator(lambda v: v[:n] if isinstance(v, str) else v)


class Person(BaseModel):
    name: Annotated[str, _clip(100)] = ""
    role: Annotated[str, _clip(100)] = ""
    email: Annotated[str, _clip(120)] = ""
    linkedin: Annotated[str, _clip(200)] = ""


class ContactResult(BaseModel):
    people: Annotated[
        list[Person], BeforeValidator(lambda v: v[:8] if isinstance(v, list) else v)
    ] = Field(default_factory=list)
    business_emails: Annotated[
        list[str], BeforeValidator(lambda v: v[:6] if isinstance(v, list) else v)
    ] = Field(default_factory=list)


async def extract_contacts(page_text: str, links: str = "") -> ContactResult:
    completion, _ = await task_runner.run_task(
        TASK,
        f"{page_text}\n\nLinks on the page:\n{links}" if links else page_text,
        schema=ContactResult,
        prompt_dir=PROMPT_DIR,
        use_examples=False,
        max_tokens=600,
        reasoning_effort="none",  # a thinking model otherwise spends the whole budget before the JSON
    )
    parsed = completion.parsed
    if not isinstance(parsed, ContactResult):
        raise TypeError("extract_contacts returned no structured result")
    return parsed
