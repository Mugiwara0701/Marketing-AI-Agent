"""Plan blog topics with the LLM."""

from pydantic import BaseModel, Field

from agentkit import task_runner

TASK = "content.plan"
PROMPT_DIR = "services/content-service/app/prompts"


class Topic(BaseModel):
    title: str = Field(min_length=5, max_length=140)
    angle: str = Field(max_length=300)
    keywords: list[str] = Field(default_factory=list, max_length=8)


class TopicPlan(BaseModel):
    topics: list[Topic] = Field(min_length=1, max_length=5)


async def plan_topics(brief: str) -> TopicPlan:
    """brief: recent posts, performance notes and knowledge highlights, as plain text."""
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
