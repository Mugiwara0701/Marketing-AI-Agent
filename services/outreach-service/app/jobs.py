"""Job implementations. Each raises NotImplementedError until built (see docs)."""

from agentkit import JobContext


async def draft(ctx: JobContext) -> dict:
    """Draft first-touch emails for approved leads; post to Slack."""
    raise NotImplementedError


async def send(ctx: JobContext) -> dict:
    """Send approved emails within daily caps; respect suppression list."""
    raise NotImplementedError


async def poll_replies(ctx: JobContext) -> dict:
    """Read the inbox, classify replies, update the CRM."""
    raise NotImplementedError


async def draft_replies(ctx: JobContext) -> dict:
    """Draft answers to replies; post to Slack for approval."""
    raise NotImplementedError


JOBS = {
    "draft": draft,
    "send": send,
    "poll_replies": poll_replies,
    "draft_replies": draft_replies,
}
