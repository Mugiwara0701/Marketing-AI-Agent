"""Phase 5: find one company's contact with the GUI agent, then use the normal lead flow.

The vision model only proposes an email. The harness re-reads the page itself (select all + copy, a
GUI-level read) and accepts the contact only if the address appears literally on that page and belongs to
the page's own company domain. Accepted leads are saved, drafted by the text LLM and posted to Slack with
Approve / Reject, exactly like discovered leads. Nothing is sent without that approval.
"""

from dataclasses import dataclass

from agentkit.log import get_logger

from .. import contacts, web
from .executor_client import ExecutorClient
from .loop import current_url, run_task
from .policy import Policy

log = get_logger("agent.gui.lead")

GOAL = (
    "Find the official website of the company '{name}' using a web search, open it, and look on its "
    "contact, about or footer area for a public business email address. When you can clearly see one on "
    "the company's own website, finish with action done and that address."
)


@dataclass
class Found:
    ok: bool
    message: str
    domain: str = ""
    email: str = ""
    name: str = ""
    role: str = ""
    page_url: str = ""


async def verify(ex: ExecutorClient, claimed_email: str) -> tuple[str, str, str]:
    """(url, domain, problem). Re-reads the page the browser is on; problem is '' when the email checks out."""
    url = await current_url(ex)
    if not Policy.url_allowed(url) or not url.startswith("http"):
        return url, "", f"browser is not on an allowed web page ({url[:60]!r})"
    domain = web.registrable_domain(url)
    if not web.is_company_site(domain):
        return url, domain, f"{domain or 'page'} is not a company website"
    text = await ex.clipboard_after("ctrl+a", "ctrl+c")
    await ex.act(action="click", x=5, y=5)  # drop the selection; harmless corner click
    if claimed_email.lower() not in contacts.emails_on_domain(text, domain):
        return url, domain, f"{claimed_email} is not on the page or not on {domain}"
    return url, domain, ""


async def find_company(
    name: str, ex: ExecutorClient | None = None, policy: Policy | None = None
) -> Found:
    ex = ex or ExecutorClient()
    outcome = await run_task(GOAL.format(name=name), ex, policy)
    if outcome.status != "done" or outcome.final is None:
        return Found(False, f"{outcome.status}: {outcome.reason} (run {outcome.run_id})")
    step = outcome.final
    email = (step.contact_email or "").strip().lower()
    url, domain, problem = await verify(ex, email)
    if problem:
        return Found(False, f"rejected: {problem} (run {outcome.run_id})")
    return Found(
        True, f"verified (run {outcome.run_id})", domain, email,
        step.contact_name or "", step.contact_role or "", url,
    )  # fmt: skip


async def save_and_post(name: str, found: Found) -> str:
    """Store the lead, draft the email, request approval (the same states and gate as discovered leads)."""
    from ..leadgen import config, service  # noqa: PLC0415 - avoids a cycle at import time
    from ..leadgen.models import Contact, Evidence, Lead  # noqa: PLC0415
    from ..leadgen.repository import open_repository  # noqa: PLC0415

    lead = Lead(
        company_name=name, company_website=found.domain, source_urls=[found.page_url],
        evidence=[Evidence(url=found.page_url, reason="company named by an operator; contact found by the GUI agent",
                           source="contact")],
    )  # fmt: skip
    contact = Contact(name=found.name, role=found.role or "business contact", email=found.email,
                      source=found.page_url, confidence=0.6, rank=13)  # fmt: skip
    repo = await open_repository(config.runtime(dry_run=False))
    try:
        _, report = await service.submit_lead(repo, lead, contact, source="gui-agent")
    finally:
        await repo.close()
    return report


async def find_many(
    names: list[str], ex: ExecutorClient | None = None
) -> list[tuple[str, Found, str]]:
    """Two passes so a single-slot model server swaps models once per batch, not twice per company:
    pass 1 finds every contact with the vision model, pass 2 drafts and posts them with the text model.
    Returns (company, found, report) per company."""
    ex = ex or ExecutorClient()
    finds = [(name, await find_company(name, ex)) for name in names]  # vision model stays loaded
    out: list[tuple[str, Found, str]] = []
    for name, found in finds:  # then the text model, for every verified contact
        report = found.message
        if found.ok:
            try:
                report = await save_and_post(name, found)
            except Exception as exc:
                log.exception("saving a GUI lead failed")
                report = f"found but not saved: {type(exc).__name__}"
        out.append((name, found, report))
    return out
