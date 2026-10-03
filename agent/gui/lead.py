"""Phase 5: find one company's contact with the GUI agent, then use the normal lead flow.

The vision model only proposes an email. The harness re-reads the page itself (select all + copy, a
GUI-level read) and accepts the contact only if the address appears literally on that page and belongs to
the page's own company domain. Accepted leads are saved, drafted by the text LLM and posted to Slack with
Approve / Skip, exactly like leads from the scripted path. Nothing is sent without that approval.
"""

from dataclasses import dataclass
from types import SimpleNamespace

from agentkit.log import get_logger

from .. import contacts, notify, store, web
from ..tasks import proposal
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
    """Save the lead, draft the email, post it to Slack. Returns a one-line report."""
    q = SimpleNamespace(
        confidence=0.7,
        reason="found by the GUI agent",
        location=None,
        technologies=[],
        project_summary="",
    )
    company_id = await store.save_company(
        name=name,
        domain=found.domain,
        status="contact_found",
        q=q,
        source="gui-agent",
        source_url=found.page_url,
        review=True,  # a person looks closely: this contact came from a vision model
    )
    contact = SimpleNamespace(email=found.email, name=found.name, role=found.role)
    contact_id = await store.save_contact(company_id, contact, found.page_url)
    ctx = (
        f"Company: {name}\nWebsite: {found.domain}\nProject: not stated\nTechnologies: not stated\n"
        f"Contact name: {found.name or 'unknown'}\nContact role: {found.role or 'business contact'}\n"
    )
    draft, problems = await proposal.draft_proposal(ctx)
    note = "; ".join(["found by GUI agent: verify the contact", *problems])
    email_id = await store.save_email_draft(contact_id, draft.subject, draft.body, note)
    if not email_id:
        return "contact saved; an intro email already exists for it"
    posted = await notify.post_email(email_id)
    return "draft posted to Slack for approval" if posted else "draft saved (Slack not posted)"


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
