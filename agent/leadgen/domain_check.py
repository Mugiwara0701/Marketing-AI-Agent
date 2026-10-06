"""One-off check of stored leads' websites (`python -m agent leads check-domains [--apply]`).

Older runs trusted the model's guess of a company's website, so some leads point at a domain that does not belong to
the company or does not answer at all (siliconsignals.com stored for a company found on siliconsignals.io).

    dead     the stored site does not answer (connection refused, closed, DNS failure, timeout): with --apply the
             lead is REJECTED, and an undecided draft is rejected as "system:domain-check" (recorded in approvals)
    suspect  the site answers, but the page the lead was found on is another company domain: reported only,
             because a company can own several domains; a person decides

One request per stored site (its home page, then www.), nothing else is fetched.
"""

from dataclasses import dataclass

import httpx

from agentkit.log import get_logger

from .. import settings
from . import identity, intent
from .models import Lead, LeadStatus
from .repository import Repository

log = get_logger("agent.leadgen.domain_check")

CHECKED = [
    LeadStatus.QUALIFIED,
    LeadStatus.CONTACT_FOUND,
    LeadStatus.EMAIL_DRAFTED,
    LeadStatus.PENDING_APPROVAL,
]
BY = "system:domain-check"


@dataclass
class Finding:
    lead_id: str
    company: str
    domain: str
    status: str  # ok | dead | suspect
    detail: str
    found_on: list[str]
    action: str = ""


async def probe(domain: str, timeout: float = 12) -> tuple[bool, str]:
    """(answers, final domain or error). Tries https://domain/ then https://www.domain/."""
    headers = {"User-Agent": settings.load().user_agent}
    error = ""
    async with httpx.AsyncClient(headers=headers, timeout=timeout, follow_redirects=True) as c:
        for host in (domain, f"www.{domain}"):
            try:
                r = await c.get(f"https://{host}/")
            except httpx.HTTPError as exc:
                error = f"{type(exc).__name__}"
                continue
            return True, intent.registrable_domain(str(r.url))
    return False, error or "no answer"


def source_domains(lead: Lead) -> list[str]:
    """Company domains of the pages the lead was found on (job boards, news sites... are left out)."""
    out = []
    for url in lead.source_urls:
        d = intent.registrable_domain(url)
        if d and identity.is_company_site(d) and d not in out:
            out.append(d)
    return out


async def judge(lead: Lead) -> Finding:
    found_on = source_domains(lead)
    f = Finding(lead.lead_id or "", lead.company_name, lead.company_website, "ok", "", found_on)
    answers, final = await probe(lead.company_website)
    if not answers:
        f.status, f.detail = "dead", f"site does not answer ({final})"
    elif found_on and lead.company_website not in found_on and final not in found_on:
        f.status, f.detail = (
            "suspect",
            f"found on {', '.join(found_on)}; stored site answers as {final}",
        )
    return f


async def run(repo: Repository, *, apply: bool = False) -> list[Finding]:
    findings = []
    for lead in await repo.leads_with_status(CHECKED, limit=2000):
        try:
            f = await judge(lead)
        except Exception as exc:  # one odd lead must not stop the check
            log.warning(
                "domain check failed",
                extra={"ctx": {"lead_id": lead.lead_id, "error": str(exc)[:120]}},
            )
            continue
        if f.status == "dead" and apply and lead.lead_id:
            f.action = await _reject(repo, lead, f.detail)
        findings.append(f)
        log.info("Domain checked", extra={"ctx": {"company": f.company, "domain": f.domain, "status": f.status,
                                                  "detail": f.detail, "action": f.action}})  # fmt: skip
    return findings


async def _reject(repo: Repository, lead: Lead, detail: str) -> str:
    assert lead.lead_id  # noqa: S101
    note = f"stored website {lead.company_website} is wrong: {detail}"
    email = await repo.email_for_lead(lead.lead_id)
    if (
        email and email.status == "drafted"
    ):  # also moves the lead to REJECTED, recorded in approvals
        return f"draft rejected ({await repo.decide_email(email.email_id, False, BY, note)})"
    ok = await repo.set_status(lead.lead_id, LeadStatus.REJECTED, note=note)
    return "lead rejected" if ok else "left as is (status does not allow rejection)"
