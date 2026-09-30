"""Job implementations. Each raises NotImplementedError until built (see docs)."""

from agentkit import JobContext


async def collect_sources(ctx: JobContext) -> dict:
    """Fetch leads from ATS feeds, company pages and manual pastes."""
    raise NotImplementedError


async def ingest_alerts(ctx: JobContext) -> dict:
    """Parse LinkedIn job-alert emails and post candidates to Slack for approval."""
    raise NotImplementedError


async def qualify(ctx: JobContext) -> dict:
    """Score candidate leads against the ICP with the LLM."""
    raise NotImplementedError


async def enrich_contacts(ctx: JobContext) -> dict:
    """Find public business contacts on approved companies' websites."""
    raise NotImplementedError


JOBS = {
    "collect_sources": collect_sources,
    "ingest_alerts": ingest_alerts,
    "qualify": qualify,
    "enrich_contacts": enrich_contacts,
}
