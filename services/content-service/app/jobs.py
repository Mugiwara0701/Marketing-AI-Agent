"""Job implementations. Each raises NotImplementedError until built (see docs)."""

from agentkit import JobContext


async def plan_topics(ctx: JobContext) -> dict:
    """Propose topics from knowledge docs and past performance."""
    raise NotImplementedError


async def draft_post(ctx: JobContext) -> dict:
    """Write a draft post with retrieval; post to Slack."""
    raise NotImplementedError


async def publish(ctx: JobContext) -> dict:
    """Publish approved posts to dev.to and LinkedIn."""
    raise NotImplementedError


async def collect_metrics(ctx: JobContext) -> dict:
    """Pull post metrics for the learning loop."""
    raise NotImplementedError


JOBS = {
    "plan_topics": plan_topics,
    "draft_post": draft_post,
    "publish": publish,
    "collect_metrics": collect_metrics,
}
