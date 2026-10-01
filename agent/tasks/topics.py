"""Propose ranked blog topics from today's research with the LLM."""

from typing import Literal

from pydantic import BaseModel, Field

from agentkit import task_runner

TASK = "content.plan"
PROMPT_DIR = "agent/prompts"


class Topic(BaseModel):
    # All required (no defaults) so schema-constrained decoding makes the model fill them in.
    title: str = Field(min_length=5, max_length=140)
    angle: str = Field(max_length=300)
    keywords: list[str] = Field(max_length=8)
    why_now: str = Field(max_length=300)
    kind: Literal["news_analysis", "explainer", "tutorial", "checklist"]
    source_ids: list[int] = Field(
        min_length=1, max_length=3
    )  # numbers of the research items it is built on


class TopicPlan(BaseModel):
    topics: list[Topic] = Field(min_length=1, max_length=5)


async def plan_topics(brief: str) -> TopicPlan:
    """brief: today's research items and our recent post titles, as plain text. Topics come back best first."""
    completion, _ = await task_runner.run_task(
        TASK,
        brief,
        schema=TopicPlan,
        prompt_dir=PROMPT_DIR,
        use_examples=False,
        use_knowledge=True,
        temperature=0.7,
        max_tokens=800,
    )
    parsed = completion.parsed
    if not isinstance(parsed, TopicPlan):
        raise TypeError("plan_topics returned no structured result")
    return parsed
