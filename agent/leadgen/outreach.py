"""Email generation: a company-specific intro written from the lead's evidence, never sent from here.

The model gets only what was found (company, product, signal, requirements, verified evidence, contact role). Code
then flags anything it could not have known: technologies not in the lead, claims about our past work, banned words.
A flagged draft still goes to approval, with the problems shown to the person deciding.
"""

import re
from pathlib import Path

from agentkit.config import env

from ..tasks import proposal
from . import intent
from .models import Contact, Lead

_SIGNAL_TEXT = {
    "rfp_or_tender": "They published a formal request (RFQ / RFP / tender) for this work.",
    "outsourcing_request": "They are asking for an outside developer / team / partner for this work.",
    "hiring": "They are hiring an engineer for this kind of work (shows the work exists; we offer to deliver it as a project, not to fill the role).",
    "product_development": "They build a product on this kind of platform. They have NOT asked for anything: make the offer conditional.",
    "partner_capacity": "They are an engineering / services company working in this area. Offer to be their subcontracting or overflow partner (white-label delivery of defined pieces such as BSP bring-up, HAL or driver work, Yocto images, OTA). Do NOT offer to replace them or compete for their customers.",
    "none": "No explicit request: make the offer conditional.",
}


def lead_context(lead: Lead, contact: Contact) -> str:
    ev = "\n".join(
        f"- {e.reason}" + (f' (page says: "{e.quote[:200]}")' if e.quote else "")
        for e in lead.evidence[:5]
    )
    return (
        f"Our company: {env('COMPANY_NAME', '') or 'our engineering team'}\n"
        f"Company: {lead.company_name}\n"
        f"Website: {lead.company_website}\n"
        f"Industry: {lead.industry or 'not stated'}\n"
        f"Product: {lead.product or 'not stated'}\n"
        f"Opportunity: {lead.opportunity_description or 'not stated'}\n"
        f"Signal: {_SIGNAL_TEXT.get(lead.project_signal, _SIGNAL_TEXT['none'])}\n"
        f"Technical requirements seen: {', '.join(lead.technical_requirements) or 'not stated'}\n"
        f"Location: {lead.location or 'not stated'}\n"
        f"Evidence:\n{ev or '- none'}\n"
        f"Contact name: {contact.name or 'unknown'}\n"
        f"Contact role: {contact.role or 'business contact'}\n"
    )


def ungrounded_tech(body: str, context: str) -> list[str]:
    """Technical terms in the email that appear nowhere in what we know about the lead (invented knowledge)."""
    out = []
    for cat, rx in intent.TECH.items():
        for m in re.finditer(rx, body, re.I):
            term = m.group(0)
            if term.lower() not in context.lower() and not re.search(rx, context, re.I):
                out.append(f"mentions {term!r} ({cat}) which was not found for this lead")
                break
    return out


def services_paragraph() -> str:
    """The "what else we provide" paragraph, from OUTREACH_SERVICES_FILE (default config/outreach_services.txt). Fixed
    text written by a person, so it never contains an invented claim; empty when the file is missing or empty."""
    path = Path(
        env("OUTREACH_SERVICES_FILE", "config/outreach_services.txt")
        or "config/outreach_services.txt"
    )
    try:
        return " ".join(path.read_text(encoding="utf-8").split())
    except OSError:
        return ""


def with_services(body: str, services: str | None = None) -> str:
    """The draft with the services paragraph placed before the closing question (the last paragraph). Added once."""
    services = services_paragraph() if services is None else services
    if not services or services in body:
        return body
    paras = re.split(r"\n\s*\n", body.strip())
    at = len(paras) - 1 if len(paras) >= 3 else len(paras)
    return "\n\n".join([*paras[:at], services, *paras[at:]])


async def draft(lead: Lead, contact: Contact) -> tuple[proposal.EmailDraft, list[str]]:
    """(draft, problems). Problems go to the approver as a warning; they never send anything."""
    ctx = lead_context(lead, contact)
    d, problems = await proposal.draft_pitch(ctx, lead.company_name)
    problems = [*problems, *ungrounded_tech(d.subject + " " + d.body, ctx)]
    if lead.company_name.lower() not in (d.subject + d.body).lower():
        problems.append("the email does not name the company: probably generic")
    # Added after the checks above: it is our own fixed text, not something the model could have invented
    return d.model_copy(update={"body": with_services(d.body)}), problems
