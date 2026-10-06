"""LLM task lead.search (desktop browser only): read the TEXT of a search-results page into a list of results.

The desktop browser cannot see the page's links, so the model lists the organic results; it does not judge them.
Judging is done per page by agent.leadgen.qualify."""

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


class SearchRead(BaseModel):
    hits: list[Hit] = Field(max_length=MAX_HITS)


async def read_results(page_text: str, query: str) -> SearchRead:
    completion, _ = await task_runner.run_task(
        TASK,
        f"Search query: {query}\n\nPage text:\n{page_text}",
        schema=SearchRead,
        prompt_dir=PROMPT_DIR,
        use_examples=False,
        max_tokens=800,
        timeout=180,
        reasoning_effort="none",
    )
    parsed = completion.parsed
    if not isinstance(parsed, SearchRead):
        raise TypeError("read_results returned no structured result")
    return parsed
