"""Qualification of one page: rules first (free), then the model (one call), then code checks on what the model
said, then the score. Returns a Verdict that says why, whatever the outcome."""

import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from agentkit.log import get_logger

from ..tasks.assess import Assessment, assess_page
from . import identity, intent, scoring
from .classify import Classification, ResultType, classify
from .config import LeadgenConfig
from .models import Evidence, Lead, Page

log = get_logger("agent.leadgen.qualify")

Assessor = Callable[[str, str, str], Awaitable[Assessment]]

_PROVIDER_PAGES = {"engineering_services_provider"}
_MAX_MODEL_TEXT = 7000


@dataclass
class Verdict:
    accepted: bool
    reason: str
    intent: intent.PageIntent
    assessment: Assessment | None = None
    lead: Lead | None = None
    classification: Classification | None = None
    used_model: bool = False
    notes: list[str] = field(default_factory=list)

    def record(self) -> dict:
        """What is stored with the page, so a person can see how it was judged."""
        out: dict = {
            "accepted": self.accepted,
            "reason": self.reason,
            "intent": self.intent.summary(),
        }
        if self.assessment:
            out["assessment"] = self.assessment.model_dump()
        if self.classification:
            out["result_type"] = self.classification.type.value
            out["customer_tier"] = self.classification.tier
        if self.lead and self.lead.score:
            out["score"] = self.lead.score.model_dump()
        return out


# --- code checks on the model's answer ----------------------------------------------------------------------


def _norm(s: str) -> str:
    quotes = "\"'`\u201c\u201d\u2018\u2019"
    return re.sub(r"\s+", " ", re.sub(f"[{quotes}]", "", s.lower())).strip(" .,:;-")


def quote_on_page(quote: str, text: str) -> bool:
    """The model's quote is really on the page (case, quotes and spacing ignored; '...' gaps allowed)."""
    page = _norm(text)
    parts = [p for p in (_norm(x) for x in re.split(r"\.{3}|…", quote)) if len(p) >= 12]
    return bool(parts) and all(p in page for p in parts)


_DATE_YEAR = re.compile(
    r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(?:\d{1,2}(?:st|nd|rd|th)?,?\s+)?(20\d\d)\b"
    r"|\b(20\d\d)-\d\d-\d\d\b|\b\d{1,2}[./]\d{1,2}[./](20\d\d)\b|/(20\d\d)/\d\d/",
    re.I,
)


def is_stale(url: str, text: str, max_age_days: int, now: float | None = None) -> bool:
    """A dated post (job, request) whose newest date is older than max_age_days. Undated pages are kept."""
    years = [int(y) for m in _DATE_YEAR.finditer(f"{url} {text}") for y in m.groups() if y]
    if not years:
        return False
    newest = datetime(max(years), 12, 31, tzinfo=UTC).timestamp()
    return (now or time.time()) - newest > max_age_days * 86400


def rule_evidence(url: str, text: str, pi: intent.PageIntent, width: int = 140) -> list[Evidence]:
    """The text around the first technical term: what a person should read to check the lead."""
    out = []
    for cat in pi.tech_terms[:2]:
        m = re.search(intent.TECH[cat], text, re.I)
        if m:
            snip = " ".join(text[max(0, m.start() - width) : m.end() + width].split())
            out.append(
                Evidence(url=url, reason=f"page mentions {cat.replace('_', ' ')}", quote=snip[:400])
            )
    return out


def requirements(needs: list[str], categories: list[str]) -> list[str]:
    """The model's named needs first, then the page's technical areas, one entry per thing (case ignored)."""
    out: list[str] = []
    for item in [*needs, *(intent.TECH_LABELS.get(c, c) for c in categories)]:
        if item and not any(item.lower() in o.lower() or o.lower() in item.lower() for o in out):
            out.append(item)
    return out[:12]


def website_for(a: Assessment, page: Page) -> str:
    """The company's own domain, only from what the page shows: the page itself when it is the company's site, or
    a domain the model read that is literally written on the page (text or links). Never guessed."""
    here = intent.registrable_domain(page.url)
    if identity.is_company_site(here) and identity.name_matches_domain(a.company_name, here):
        return here
    claimed = intent.registrable_domain(a.company_website) if a.company_website else ""
    if claimed and identity.is_company_site(claimed):
        seen = page.text.lower() + " " + " ".join(link.url.lower() for link in page.links)
        if claimed in seen:
            return claimed
    for link in page.links:  # the company's site linked from a job board / request page
        dom = intent.registrable_domain(link.url)
        if (
            dom != here
            and identity.is_company_site(dom)
            and identity.name_matches_domain(a.company_name, dom)
        ):
            return dom
    return ""


# --- the step ------------------------------------------------------------------------------------------------


def company_problem(
    a: Assessment, page: Page, host: str, cfg: LeadgenConfig, kind: Classification
) -> str | None:
    """Why the organisation the model named cannot be a lead, or None: not a company, not really on the page, the
    platform itself, an excluded company, or no signal at all."""
    name = a.company_name.strip()
    if intent.NOT_A_COMPANY.search(name):
        return f"'{name}' is not a company that buys engineering (foundation, university, association...)"
    if not name or not identity.name_on_page(name, f"{page.title} {page.text}"):
        return "no identifiable company on the page"
    key = identity.name_key(name)
    if (
        key
        and key == identity.name_key(host.split(".", maxsplit=1)[0])
        and not identity.is_company_site(host)
    ):
        return f"'{name}' is the platform itself, not a company with a need"
    if a.project_signal == "hiring" and not cfg.q("accept_hiring_signals", True):
        return "hiring post (hiring signals are switched off)"
    if a.project_signal == "none" and kind.proceed and kind.type != ResultType.POTENTIAL_CUSTOMER:
        return f"{kind.type} without any project, hiring or product-development signal"
    return intent.exclusion_reason("", "", name, [], cfg.exclude_companies) or None


async def qualify_page(  # noqa: PLR0911 - one rule per branch, each with its own reason
    page: Page, cfg: LeadgenConfig, assess: Assessor = assess_page
) -> Verdict:
    pi = intent.analyze(page.url, page.title, page.text)
    host = intent.registrable_domain(page.url)
    services = bool(cfg.q("accept_service_companies", True))
    if why := intent.prefilter(
        pi, min_relevance=int(cfg.q("min_relevance_terms", 2)), accept_services=services
    ):
        return Verdict(False, why, pi)
    if why := intent.exclusion_reason(
        f"{page.title} {page.text[:4000]}", host, "", cfg.exclude_terms, cfg.exclude_companies
    ):
        return Verdict(False, why, pi)
    dated = pi.project_request >= 2 or pi.employment >= 2
    if dated and is_stale(page.url, page.text, int(cfg.q("max_age_days", 120))):
        return Verdict(False, "dated post older than max_age_days", pi)

    a = await assess(page.url, page.title, page.text[:_MAX_MODEL_TEXT])
    v = Verdict(False, "", pi, a, used_model=True)
    name = a.company_name.strip()
    accept_hiring = bool(cfg.q("accept_hiring_signals", True))
    kind = classify(a, pi, accept_services=services)
    v.classification = kind
    # An informational page (news, directory...) never creates a lead, but about a company we already have it is one
    # more source of evidence: it is read on and handed over with accepted = False.
    evidence_only = kind.type == ResultType.INFORMATIONAL
    if not kind.proceed and not evidence_only:
        v.reason = f"{kind.type}: {kind.why}"
        return v
    if (
        services
        and a.project_signal in ("none", "product_development")
        and (a.page_type in _PROVIDER_PAGES or pi.provider >= 3)
    ):
        a.project_signal = (
            "partner_capacity"  # configured partner lead: a possible subcontracting partner
        )
    if why := company_problem(a, page, host, cfg, kind):
        v.reason = why
        return v
    website = website_for(a, page)
    if why := intent.country_excluded(website, a.location, cfg.exclude_countries, cfg.exclude_tlds):
        v.reason = why
        return v

    verified = [q for q in a.evidence if quote_on_page(q.quote, page.text)]
    if len(verified) < len(a.evidence):
        v.notes.append(
            f"{len(a.evidence) - len(verified)} model quote(s) not found on the page, dropped"
        )
    evidence = [
        Evidence(url=page.url, reason=q.reason, quote=q.quote, source="llm") for q in verified
    ] + rule_evidence(page.url, page.text, pi)
    lead = Lead(
        company_name=name,
        company_website=website,
        name_key=identity.name_key(name),
        industry=a.industry,
        product=a.product,
        project_description=a.opportunity,
        technical_requirements=requirements(a.engineering_needs, pi.tech_terms),
        opportunity_description=a.opportunity,
        project_signal=a.project_signal,
        page_type=a.page_type,
        result_type=kind.type.value,
        customer_tier=kind.tier,
        location=a.location,
        source_urls=[page.url],
        qualification_notes=v.notes,
    )
    lead.add_evidence(evidence)
    card = scoring.score(
        scoring.ScoreInput(
            assessment=a, intent=pi, verified_quotes=len(verified),
            name_on_page=True, website_known=bool(website), accept_hiring=accept_hiring,
            accept_services=services, result_type=kind.type, tier=kind.tier,
        )
    )  # fmt: skip
    lead.score, lead.lead_score = card, card.total
    v.lead = lead
    if evidence_only:
        v.reason = f"{kind.type}: {kind.why} (kept only as evidence for a company already known)"
        return v
    min_conf = float(cfg.q("min_confidence", 0.5))
    min_score = int(cfg.q("min_qualify", 50))
    if a.confidence < min_conf:
        v.reason = f"model confidence {a.confidence:.2f} below {min_conf}"
    elif card.total < min_score:
        v.reason = f"score {card.total} below {min_score}"
    else:
        v.accepted, v.reason = True, f"score {card.total}"
    return v
