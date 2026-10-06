"""Lead score, 0-100, from six dimensions with explicit penalties, so a person can always see WHY.

    company relevance     20  builds its own relevant product / system
    technical relevance   20  our stack is visibly involved (AOSP, BSP, kernel, Yocto, firmware, SoCs...)
    project signal        25  RFQ / outsourcing > hiring > product development > nothing
    commercial potential  15  identifiable company, own website, a page type that can become a B2B project
    contactability        10  decision maker > named person > business mailbox > contact form
    evidence quality      10  verified quotes from the page, several sources

Selling, reselling and second-hand pages are penalised hard: quality over quantity.
"""

from dataclasses import dataclass

from ..tasks.assess import Assessment
from .intent import PageIntent
from .models import Contact, ScoreCard

_SIGNAL_POINTS = {
    "rfp_or_tender": 25,
    "outsourcing_request": 22,
    "hiring": 14,
    "product_development": 8,
    "partner_capacity": 11,
    "none": 0,
}
_PAGE_PENALTY = {
    "ecommerce_listing": 40,
    "marketplace": 40,
    "distributor_or_reseller": 30,
    "documentation_or_tutorial": 30,
    "directory": 30,
    "forum_discussion": 15,
    "job_aggregator": 10,
    "news_article": 10,
    "engineering_services_provider": 40,
}
DECISION_MAKER_RANK = 6  # contacts.ROLE_PRIORITY: CTO ... co-founder


@dataclass
class ScoreInput:
    assessment: Assessment
    intent: PageIntent
    verified_quotes: int
    name_on_page: bool
    website_known: bool
    source_count: int = 1
    contact: Contact | None = None
    form_only: bool = False
    accept_hiring: bool = True
    accept_services: bool = False
    result_type: str = "POTENTIAL_CUSTOMER"
    tier: str = "potential"


def _project(s: ScoreInput) -> tuple[int, str | None]:
    a, i = s.assessment, s.intent
    llm = _SIGNAL_POINTS[a.project_signal]
    if a.project_signal == "hiring" and not s.accept_hiring:
        llm = 0
    rule = 0
    if i.project_request >= 2:
        rule = 18
    elif i.employment >= 2 and s.accept_hiring:
        rule = 10
    elif i.product_dev >= 2:
        rule = 6
    note = "project signal read from the page wording, not by the model" if rule > llm else None
    return max(llm, rule), note


def contact_points(c: Contact | None, form_only: bool = False) -> int:
    if c and c.email:
        if c.name and c.rank <= DECISION_MAKER_RANK:
            return 10
        if c.name:
            return 8
        return 6
    return 3 if form_only else 0


def score(s: ScoreInput) -> ScoreCard:
    a, i = s.assessment, s.intent
    notes: list[str] = []
    partner = s.accept_services and a.project_signal == "partner_capacity"
    company = (12 if a.builds_own_product or partner else 0) + min(
        len(i.domains) + bool(a.industry), 2
    ) * 4
    # Our stack named on the page, or (for a device maker that does not name it) the hardware it builds.
    technical = min(len(i.tech), 4) * 4 + (4 if a.engineering_needs else 0) + min(i.hardware, 3) * 3
    project, note = _project(s)
    if note:
        notes.append(note)
    commercial = (
        (5 if a.company_name and s.name_on_page else 0)
        + (5 if s.website_known else 0)
        + (5 if a.page_type in ("company_product_page", "project_request", "hiring_post") else 0)
    )
    evidence = (
        min(s.verified_quotes, 2) * 3 + (2 if i.tech else 0) + (2 if s.source_count > 1 else 0)
    )
    parts = {
        "company_relevance": min(company, 20),
        "technical_relevance": min(technical, 20),
        "project_signal": min(project, 25),
        "commercial_potential": min(commercial, 15),
        "contactability": contact_points(s.contact, s.form_only),
        "evidence_quality": min(evidence, 10),
    }
    asks = a.project_signal in ("rfp_or_tender", "outsourcing_request")
    penalties: dict[str, int] = {}
    second_hand_request = asks and a.page_type in (
        "forum_discussion",
        "marketplace",
        "news_article",
    )
    penalty = _PAGE_PENALTY.get(a.page_type)
    if a.page_type == "engineering_services_provider" and s.accept_services:
        penalty = None  # a partner lead, not a competitor
    if (p := penalty) and not second_hand_request:
        penalties[f"page type: {a.page_type}"] = p
    if i.selling >= 4 and not asks:
        penalties["sells products (cart, prices)"] = 40
    elif i.selling >= 2 and not asks:
        penalties["some shop features"] = 15
    if i.distributor >= 2 and not asks:
        penalties["distributor / reseller wording"] = 30
    makes_things = i.product_dev or i.hardware >= 3
    if i.provider >= 2 and not i.asks and not s.accept_services and not makes_things:
        penalties["services-company wording (possible competitor)"] = 20
    if s.result_type == "HARDWARE_MANUFACTURER":
        penalties["hardware maker, no evidence yet of embedded software work"] = 15
    if a.sells_hardware_only:
        penalties["only sells hardware"] = 30
    if not s.verified_quotes:
        penalties["no verifiable quote from the page"] = 10
    if a.confidence < 0.5:
        penalties[f"model unsure ({a.confidence:.2f})"] = 10
    if not a.company_name or not s.name_on_page:
        penalties["no identifiable company on the page"] = 25
    return ScoreCard(total=_total(parts, penalties), parts=parts, penalties=penalties, notes=notes)


def _total(parts: dict[str, int], penalties: dict[str, int]) -> int:
    return max(0, min(100, sum(parts.values()) - sum(penalties.values())))


def with_contact(card: ScoreCard, contact: Contact | None, *, form_only: bool = False) -> ScoreCard:
    """The same score with contactability filled in (contact discovery runs after qualification)."""
    parts = {**card.parts, "contactability": contact_points(contact, form_only)}
    return ScoreCard(
        total=_total(parts, card.penalties), parts=parts, penalties=card.penalties, notes=card.notes
    )


def merge(old: ScoreCard | None, new: ScoreCard | None, sources: int) -> ScoreCard | None:
    """Two pages about the same company: the stronger reading of each dimension, the milder penalties, and the
    evidence bonus for several independent sources."""
    if old is None or new is None:
        return old or new
    parts = {k: max(old.parts.get(k, 0), new.parts.get(k, 0)) for k in {*old.parts, *new.parts}}
    if sources > 1:
        parts["evidence_quality"] = min(10, parts.get("evidence_quality", 0) + 2)
    penalties = {k: v for k, v in old.penalties.items() if k in new.penalties}
    notes = list(dict.fromkeys([*old.notes, *new.notes]))
    return ScoreCard(total=_total(parts, penalties), parts=parts, penalties=penalties, notes=notes)
