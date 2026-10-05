"""Read the text of a search-results page and pick the results worth opening, plus follow-up queries."""

from pydantic import BaseModel, Field

from agentkit import task_runner

TASK = "lead.search"
PROMPT_DIR = "agent/prompts"
MAX_HITS = 10


class Hit(BaseModel):
    title: str = Field(
        max_length=200
    )  # exactly as written on the page, so it can be found on screen
    domain: str = Field(max_length=100)  # site shown under the title, or empty
    snippet: str = Field(max_length=300)
    likely_project: bool  # a company needing outside engineering work, not a job post or an article
    score: float = Field(ge=0, le=1)


class SearchRead(BaseModel):
    hits: list[Hit] = Field(max_length=MAX_HITS)
    next_queries: list[str] = Field(max_length=4)


async def read_results(page_text: str, query: str) -> SearchRead:
    completion, _ = await task_runner.run_task(
        TASK,
        f"Search query: {query}\n\nPage text:\n{page_text}",
        schema=SearchRead,
        prompt_dir=PROMPT_DIR,
        use_examples=False,
        max_tokens=1200,
        timeout=120,
        reasoning_effort="none",
    )
    parsed = completion.parsed
    if not isinstance(parsed, SearchRead):
        raise TypeError("read_results returned no structured result")
    return parsed
