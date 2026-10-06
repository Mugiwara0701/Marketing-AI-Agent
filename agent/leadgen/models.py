"""Data model of the lead pipeline: the lead state machine, leads, contacts, evidence, approvals.

Every module of agent.leadgen speaks these types; the repositories map them onto tables.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


class LeadStatus(StrEnum):
    DISCOVERED = "DISCOVERED"  # a company was identified from a page; not judged yet
    QUALIFIED = "QUALIFIED"  # the evidence says it may buy our engineering services
    CONTACT_FOUND = "CONTACT_FOUND"  # a public business contact is known
    EMAIL_DRAFTED = "EMAIL_DRAFTED"  # an intro email is drafted; no human has seen it yet
    PENDING_APPROVAL = "PENDING_APPROVAL"  # the draft was put in front of a human (Slack)
    APPROVED = "APPROVED"  # a human approved the draft: the ONLY state from which mail may be sent
    REJECTED = "REJECTED"  # not a lead, or a human rejected the draft
    SENT = "SENT"
    FAILED = "FAILED"  # sending failed for good


# Allowed transitions. Anything else is a bug and raises InvalidTransitionError.
TRANSITIONS: dict[LeadStatus, frozenset[LeadStatus]] = {
    LeadStatus.DISCOVERED: frozenset({LeadStatus.QUALIFIED, LeadStatus.REJECTED}),
    LeadStatus.QUALIFIED: frozenset({LeadStatus.CONTACT_FOUND, LeadStatus.REJECTED}),
    LeadStatus.CONTACT_FOUND: frozenset({LeadStatus.EMAIL_DRAFTED, LeadStatus.REJECTED}),
    LeadStatus.EMAIL_DRAFTED: frozenset(
        {LeadStatus.PENDING_APPROVAL, LeadStatus.APPROVED, LeadStatus.REJECTED}
    ),
    LeadStatus.PENDING_APPROVAL: frozenset({LeadStatus.APPROVED, LeadStatus.REJECTED}),
    LeadStatus.APPROVED: frozenset({LeadStatus.SENT, LeadStatus.FAILED}),
    # New evidence from another page may turn a rejected company into a lead after all.
    LeadStatus.REJECTED: frozenset({LeadStatus.QUALIFIED}),
    LeadStatus.SENT: frozenset(),
    LeadStatus.FAILED: frozenset({LeadStatus.APPROVED}),  # a person may retry a failed send
}


class InvalidTransitionError(RuntimeError):
    pass


def check_transition(current: LeadStatus, new: LeadStatus) -> None:
    if new not in TRANSITIONS[current]:
        raise InvalidTransitionError(f"lead cannot go from {current} to {new}")


def sources_of(target: LeadStatus) -> frozenset[LeadStatus]:
    """Every state that may move to `target` (used for conditional, race-free updates)."""
    return frozenset(s for s, nxt in TRANSITIONS.items() if target in nxt)


PageType = Literal[
    "company_product_page",
    "project_request",
    "hiring_post",
    "ecommerce_listing",
    "distributor_or_reseller",
    "marketplace",
    "job_aggregator",
    "news_article",
    "documentation_or_tutorial",
    "forum_discussion",
    "directory",
    "engineering_services_provider",
    "other",
]
ProjectSignal = Literal[
    "none",
    "product_development",
    "partner_capacity",
    "hiring",
    "outsourcing_request",
    "rfp_or_tender",
]


class Evidence(BaseModel):
    """One reason to believe the company is a potential customer, with where it was seen."""

    url: str
    reason: str = Field(max_length=300)
    quote: str = Field(default="", max_length=400)  # literal text from the page, when there is one
    source: Literal["llm", "rule", "contact"] = "rule"


class Contact(BaseModel):
    name: str = ""
    role: str = ""
    email: str = ""
    linkedin: str = (
        ""  # only a link that is written on the company's own page; LinkedIn is never fetched
    )
    source: str = ""  # URL of the page the contact was read from
    confidence: float = 0.0
    rank: int = 99  # role priority: 0 = CTO ... lower is better (see contacts.ROLE_PRIORITY)
    id: str | None = None


class ScoreCard(BaseModel):
    total: int
    parts: dict[str, int]
    penalties: dict[str, int] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)


class Lead(BaseModel):
    company_name: str
    company_website: str = ""  # registrable domain, e.g. "acme-ev.com"
    name_key: str = ""  # normalized company name (identity.name_key)
    industry: str = ""
    product: str = ""
    project_description: str = ""
    technical_requirements: list[str] = Field(default_factory=list)
    opportunity_description: str = ""
    project_signal: ProjectSignal = "none"
    page_type: PageType = "other"
    result_type: str = "UNKNOWN"  # classify.ResultType: what kind of organisation this is
    customer_tier: str = "none"  # high (customer + engineering need) | potential | investigate
    location: str = ""
    lead_score: int = 0
    score: ScoreCard | None = None
    qualification_notes: list[str] = Field(default_factory=list)
    status: LeadStatus = LeadStatus.DISCOVERED
    source_urls: list[str] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    contact: Contact | None = None
    email_draft_id: str | None = None
    approval_status: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    lead_id: str | None = None

    def add_evidence(self, items: list[Evidence]) -> None:
        """Merge evidence, one entry per (url, quote or reason)."""
        seen = {(e.url, e.quote or e.reason) for e in self.evidence}
        for e in items:
            key = (e.url, e.quote or e.reason)
            if key not in seen:
                seen.add(key)
                self.evidence.append(e)
        for e in items:  # where the lead was found; a contact page is not a source of the lead
            if e.url and e.source != "contact" and e.url not in self.source_urls:
                self.source_urls.append(e.url)


class Approval(BaseModel):
    email_id: str
    lead_id: str | None = None
    status: Literal["pending", "approved", "rejected", "expired"] = "pending"
    slack_channel: str | None = None
    slack_message_id: str | None = None
    approved_by: str | None = None
    approved_at: datetime | None = None
    rejected_by: str | None = None
    rejected_at: datetime | None = None


class EmailDraft(BaseModel):
    email_id: str
    lead_id: str
    contact_id: str
    to: str
    subject: str
    body: str
    review_note: str | None = None
    status: str = "drafted"  # drafted | approved | sending | sent | skipped | expired
    step: int = 1
    attempts: int = 0
    in_reply_to: str | None = None  # follow-ups and replies thread under the mail they answer


# --- what the browser layer hands to the rest of the pipeline ------------------------------------------


@dataclass(frozen=True)
class Link:
    text: str
    url: str


@dataclass
class SearchResult:
    title: str
    url: str = ""  # empty when the browser cannot see it (desktop OCR); opened by its title then
    snippet: str = ""
    engine: str = ""
    domain: str = ""


@dataclass
class Page:
    url: str
    title: str
    text: str
    links: list[Link] = field(default_factory=list)
    blocked: str | None = (
        None  # CAPTCHA / bot check / login wall / access denied, when the page is one
    )
    has_contact_form: bool = False  # a form with a free-text message box (contact-by-hand fallback)


@dataclass
class Observation:
    """One page the research agent read, with how it got there."""

    query: str
    page: Page
    result: SearchResult | None = None
    origin: str = "search"  # search | feed:<name>
