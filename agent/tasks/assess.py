"""LLM task lead.assess: read one page and say whether it shows a company that may BUY our engineering services.

The model reads; code decides. The answer is a structured reading of the page (what kind of page, which company,
what it builds, what signal of need). Scoring, thresholds and every hard rule live in agent.leadgen.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, Field

from agentkit import task_runner

TASK = "lead.assess"
PROMPT_DIR = "agent/prompts"


def _clip(n: int):
    """Over-long text is cut, not rejected: one long field must not throw away (and re-ask for) a whole reading."""
    return BeforeValidator(lambda v: v[:n] if isinstance(v, str) else v)


class Quote(BaseModel):
    quote: Annotated[
        str, _clip(400)
    ]  # copied word for word from the page (checked by code; a cut quote still matches)
    reason: Annotated[str, _clip(300)]


class Assessment(BaseModel):
    page_type: Literal[
        "company_product_page", "project_request", "hiring_post", "ecommerce_listing",
        "distributor_or_reseller", "marketplace", "job_aggregator", "news_article",
        "documentation_or_tutorial", "forum_discussion", "directory", "engineering_services_provider", "other",
    ]  # fmt: skip
    company_name: Annotated[str, _clip(120)]
    company_website: Annotated[str, _clip(120)]
    industry: Annotated[str, _clip(80)]
    product: Annotated[str, _clip(200)]
    company_role: Literal[
        "builds_end_products", "sells_components_or_modules", "sells_engineering_services",
        "resells_or_distributes", "unknown",
    ] = "unknown"  # fmt: skip
    builds_own_product: bool
    sells_hardware_only: bool
    project_signal: Literal[
        "none",
        "product_development",
        "partner_capacity",
        "hiring",
        "outsourcing_request",
        "rfp_or_tender",
    ]
    engineering_needs: Annotated[
        list[str], BeforeValidator(lambda v: v[:10] if isinstance(v, list) else v)
    ]
    opportunity: Annotated[str, _clip(600)]
    location: Annotated[str, _clip(100)]
    evidence: Annotated[list[Quote], BeforeValidator(lambda v: v[:4] if isinstance(v, list) else v)]
    confidence: float = Field(ge=0, le=1)


async def assess_page(url: str, title: str, text: str) -> Assessment:
    completion, _ = await task_runner.run_task(
        TASK,
        f"URL: {url}\nTitle: {title}\n\n{text}",
        schema=Assessment,
        prompt_dir=PROMPT_DIR,
        use_examples=False,
        max_tokens=900,
        timeout=180,
        reasoning_effort="none",  # a thinking model otherwise spends the budget before the JSON
    )
    parsed = completion.parsed
    if not isinstance(parsed, Assessment):
        raise TypeError("lead.assess returned no structured result")
    return parsed
