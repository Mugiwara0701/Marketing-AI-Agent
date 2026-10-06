"""Result-type classification: WHAT KIND OF ORGANISATION a page shows, before it may become a lead.

The business target never changes: organisations that could BUY our embedded / AOSP / BSP / HAL / Linux / Yocto /
firmware / application engineering. Technologies on a page (RFID, cameras, CAN, GPS, Jetson...) are context about
the product, never a reason to target the companies that sell them. So the question asked here is not "is the page
about our technology?" but "what does this company do?":

    sells a component / module / board        HARDWARE_SUPPLIER     rejected
    builds a complete product or system       POTENTIAL_CUSTOMER    qualified (tier "potential")
      ... and shows an engineering need       POTENTIAL_CUSTOMER    qualified (tier "high")
    makes hardware, no sign of software work  HARDWARE_MANUFACTURER investigated (penalised, needs more evidence)
    sells the engineering we sell             SERVICE_COMPANY       rejected, unless it asks for subcontractors
    shop / marketplace / reseller             ECOMMERCE / RESELLER  rejected
    job ad / freelance post                   JOB_POSTING / FREELANCE_PROJECT: the company behind it is the lead
    docs / news / forum / directory           INFORMATIONAL         rejected

The model reads the page (tasks/assess.py: page_type, company_role, project_signal); the rules here combine that
reading with what the page text shows (intent.PageIntent). No technology is special-cased.
"""

from dataclasses import dataclass
from enum import StrEnum

from ..tasks.assess import Assessment
from .intent import PageIntent


class ResultType(StrEnum):
    POTENTIAL_CUSTOMER = "POTENTIAL_CUSTOMER"
    SERVICE_COMPANY = "SERVICE_COMPANY"
    HARDWARE_SUPPLIER = "HARDWARE_SUPPLIER"
    HARDWARE_MANUFACTURER = "HARDWARE_MANUFACTURER"
    RESELLER = "RESELLER"
    ECOMMERCE = "ECOMMERCE"
    JOB_POSTING = "JOB_POSTING"
    FREELANCE_PROJECT = "FREELANCE_PROJECT"
    INFORMATIONAL = "INFORMATIONAL"
    IRRELEVANT = "IRRELEVANT"
    UNKNOWN = "UNKNOWN"


# What each type means for the pipeline.
PROCEED = frozenset({
    ResultType.POTENTIAL_CUSTOMER, ResultType.JOB_POSTING, ResultType.FREELANCE_PROJECT,
    ResultType.HARDWARE_MANUFACTURER,  # investigated: kept only if the rest of the evidence carries it
})  # fmt: skip
REJECT_REASON = {
    ResultType.SERVICE_COMPANY: "service company: sells the engineering we sell (a competitor), asks for nothing",
    ResultType.HARDWARE_SUPPLIER: "hardware / component supplier: sells parts others build into products",
    ResultType.RESELLER: "reseller / distributor of other brands' products",
    ResultType.ECOMMERCE: "ecommerce / marketplace listing",
    ResultType.INFORMATIONAL: "informational page (docs, news, forum, directory): no customer on it",
    ResultType.IRRELEVANT: "irrelevant: no organisation that could need our engineering",
    ResultType.UNKNOWN: "unknown: not enough evidence of what the company does",
}
# Signals that the organisation needs engineering it does not have in-house.
NEED_SIGNALS = frozenset({"hiring", "outsourcing_request", "rfp_or_tender", "partner_capacity"})
_ASK_SIGNALS = frozenset({"outsourcing_request", "rfp_or_tender"})
_INFO_PAGES = frozenset(
    {"documentation_or_tutorial", "news_article", "forum_discussion", "directory"}
)


@dataclass(frozen=True)
class Classification:
    type: ResultType
    tier: str  # "high" (customer + engineering need) | "potential" | "investigate" | "none"
    why: str

    @property
    def proceed(self) -> bool:
        return self.type in PROCEED


def _software_evidence(a: Assessment, pi: PageIntent) -> bool:
    """The product has software we could work on: our stack named, needs read by the model, or a device platform."""
    return bool(pi.tech or a.engineering_needs or a.project_signal in NEED_SIGNALS)


def classify(a: Assessment, pi: PageIntent, *, accept_services: bool = False) -> Classification:  # noqa: PLR0911
    asks = a.project_signal in _ASK_SIGNALS and (pi.asks > 0 or a.page_type == "project_request")
    need = a.project_signal in NEED_SIGNALS

    if asks:  # someone asks for outside work: whoever they are, the request is the opportunity
        if pi.host_kind == "freelance":
            return Classification(
                ResultType.FREELANCE_PROJECT, "high", "a project posted for outside developers"
            )
        return Classification(ResultType.POTENTIAL_CUSTOMER, "high", "asks for outside engineering")
    if a.page_type in ("ecommerce_listing", "marketplace") or pi.host_kind == "marketplace":
        return Classification(ResultType.ECOMMERCE, "none", REJECT_REASON[ResultType.ECOMMERCE])
    if a.page_type == "distributor_or_reseller" or a.company_role == "resells_or_distributes":
        return Classification(ResultType.RESELLER, "none", REJECT_REASON[ResultType.RESELLER])
    service = (
        a.page_type == "engineering_services_provider"
        or a.company_role == "sells_engineering_services"
        or pi.provider >= 3
    )
    if service:
        if accept_services:  # configured: engineering firms as subcontracting-partner leads
            return Classification(
                ResultType.POTENTIAL_CUSTOMER, "potential", "engineering firm (partner lead)"
            )
        return Classification(
            ResultType.SERVICE_COMPANY, "none", REJECT_REASON[ResultType.SERVICE_COMPANY]
        )
    if a.page_type in _INFO_PAGES:
        return Classification(
            ResultType.INFORMATIONAL, "none", REJECT_REASON[ResultType.INFORMATIONAL]
        )
    if a.page_type in ("hiring_post", "job_aggregator") or a.project_signal == "hiring":
        tier = "high" if _software_evidence(a, pi) else "potential"
        return Classification(
            ResultType.JOB_POSTING, tier, "a job ad: the hiring company is the lead"
        )
    if a.company_role == "sells_components_or_modules" or a.sells_hardware_only:
        return Classification(
            ResultType.HARDWARE_SUPPLIER, "none", REJECT_REASON[ResultType.HARDWARE_SUPPLIER]
        )
    if a.company_role == "builds_end_products" or a.builds_own_product:
        if _software_evidence(a, pi) or pi.hardware >= 3:
            tier = "high" if need else "potential"
            return Classification(
                ResultType.POTENTIAL_CUSTOMER,
                tier,
                "builds a product with software we could work on",
            )
        return Classification(ResultType.HARDWARE_MANUFACTURER, "investigate",
                              "makes hardware; no evidence yet of embedded software work")  # fmt: skip
    if not a.company_name:
        return Classification(ResultType.IRRELEVANT, "none", REJECT_REASON[ResultType.IRRELEVANT])
    return Classification(ResultType.UNKNOWN, "none", REJECT_REASON[ResultType.UNKNOWN])
