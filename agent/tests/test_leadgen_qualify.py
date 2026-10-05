"""Qualification and scoring: what the model says is checked by code; sellers never become leads."""

import asyncio

from agent.leadgen import qualify, scoring
from agent.leadgen.intent import analyze
from agent.leadgen.models import Contact, Link, Page
from agent.tests.leadgen_fakes import ScriptedModel, assessment, cfg

EV_TEXT = (
    "VoltGrid Energy designs and manufactures DC fast chargers for bus fleets. Our charger runs embedded Linux on an "
    "i.MX8 controller board with CAN bus and UART links to the power modules. Our engineering team is developing "
    "the next-generation platform with OTA updates. Contact: sales@voltgrid.com"
)
RFQ_TEXT = (
    "Kioskly builds self-service ordering kiosks on Rockchip RK3568 boards. We are looking for an outside "
    "engineering team to customize AOSP 13 for our board: board bring-up, a HAL for our card reader and a "
    "locked-down launcher. Fixed-price contract project; submit your proposal by 30 November."
)
SHOP_TEXT = (
    "Industrial Android development board available for ₹15,000. RK3568 embedded Linux and Android BSP. "
    "Add to cart. Ships within 3 days."
)


def _run(page, model, **conf):
    return asyncio.run(qualify.qualify_page(page, cfg(**conf), model))


def test_product_company_is_qualified_with_verified_evidence():
    page = Page("https://voltgrid.com/", "VoltGrid Energy", EV_TEXT)
    model = ScriptedModel({page.url: assessment(
        company_name="VoltGrid Energy", industry="EV charging", product="DC fast chargers",
        engineering_needs=["embedded Linux", "device drivers", "OTA"],
        opportunity="Embedded Linux platform work for their charger controller.",
        evidence=[{"quote": "Our charger runs embedded Linux on an i.MX8 controller board", "reason": "Linux device"},
                  {"quote": "We are looking for Linux experts", "reason": "invented"}],
    )})  # fmt: skip
    v = _run(page, model)
    assert v.accepted, v.reason
    lead = v.lead
    assert lead and lead.company_website == "voltgrid.com"  # the page is the company's own site
    llm_quotes = [e for e in lead.evidence if e.source == "llm"]
    assert len(llm_quotes) == 1  # the invented quote was dropped
    assert "not found on the page" in " ".join(lead.qualification_notes)
    assert lead.score and lead.score.parts["project_signal"] == 8


def test_seller_page_never_reaches_the_model():
    page = Page("https://boards.example/rk3568", "RK3568 board", SHOP_TEXT)
    model = ScriptedModel({})
    v = _run(page, model)
    assert not v.accepted and "ecommerce" in v.reason and model.calls == []


def test_model_reading_a_seller_is_rejected_even_if_relevant():
    text = "Rugged Android handheld terminals with embedded Linux BSP. We are a trader and exporter of handhelds."
    page = Page("https://trader.example/", "Handhelds", text)
    model = ScriptedModel({page.url: assessment(page_type="distributor_or_reseller", company_name="Trader",
                                                 sells_hardware_only=True)})  # fmt: skip
    v = _run(page, model)
    assert not v.accepted and "seller" in v.reason


def test_invented_company_is_rejected():
    page = Page("https://voltgrid.com/", "Home", EV_TEXT)
    model = ScriptedModel({page.url: assessment(company_name="Nonexistent Motors")})
    v = _run(page, model)
    assert not v.accepted and "no identifiable company" in v.reason


def test_platform_name_is_not_the_company():
    page = Page(
        "https://www.upwork.com/jobs/aosp", "Upwork", RFQ_TEXT.replace("Kioskly", "Upwork client")
    )
    model = ScriptedModel({page.url: assessment(page_type="project_request", company_name="Upwork",
                                                 project_signal="outsourcing_request")})  # fmt: skip
    v = _run(page, model)
    assert not v.accepted and "platform itself" in v.reason


def test_rfq_scores_above_hiring_above_product():
    page = Page(
        "https://voltgrid.com/", "VoltGrid Energy", EV_TEXT
    )  # wording without a request of its own
    scores = {}
    for signal in ("rfp_or_tender", "outsourcing_request", "hiring", "product_development"):
        model = ScriptedModel({page.url: assessment(
            company_name="VoltGrid Energy", project_signal=signal, engineering_needs=["embedded Linux"],
            evidence=[{"quote": "Our charger runs embedded Linux", "reason": "Linux device"}],
        )})  # fmt: skip
        v = _run(page, model)
        scores[signal] = v.lead.lead_score if v.lead else -1
    assert scores["rfp_or_tender"] > scores["outsourcing_request"] > scores["hiring"]
    assert scores["hiring"] > scores["product_development"] > 0


def test_page_wording_backs_up_a_model_that_missed_the_request():
    page = Page("https://kioskly.io/partners", "Kioskly partners", RFQ_TEXT)
    model = ScriptedModel({page.url: assessment(page_type="project_request", company_name="Kioskly",
                                                 project_signal="none", builds_own_product=True)})  # fmt: skip
    v = _run(page, model)
    assert v.lead and v.lead.score and v.lead.score.parts["project_signal"] == 18
    assert "not by the model" in v.lead.score.notes[0]


def test_hiring_signals_can_be_switched_off():
    text = "MediSense designs patient monitors on embedded Linux and i.MX8. We are hiring an Embedded Linux Engineer. Full-time. Apply now."
    page = Page("https://medisense.io/careers/1", "Careers", text)
    model = ScriptedModel({page.url: assessment(page_type="hiring_post", company_name="MediSense",
                                                 project_signal="hiring")})  # fmt: skip
    assert "switched off" in _run(page, model, accept_hiring_signals=False).reason
    assert _run(page, model).lead is not None


def test_website_is_never_guessed():
    page = Page(
        "https://jobs.example/123", "Job", RFQ_TEXT, links=[Link("Kioskly", "https://kioskly.io/")]
    )
    a = assessment(company_name="Kioskly", company_website="kioskly.com")  # not on the page
    assert qualify.website_for(a, page) == "kioskly.io"  # the linked site, not the model's guess
    assert qualify.website_for(a, Page("https://jobs.example/1", "", RFQ_TEXT)) == ""


def test_quotes_tolerate_spacing_and_ellipsis_but_not_invention():
    text = "We  build “smart” kiosks. They run Android on RK3568 boards and talk to printers over UART."
    assert qualify.quote_on_page('we build "smart" kiosks', text)
    assert qualify.quote_on_page("They run Android on RK3568 ... printers over UART", text)
    assert not qualify.quote_on_page("They run Linux on i.MX8 boards", text)


def test_stale_dated_posts():
    assert qualify.is_stale("https://x/2019/05/rfq", "posted May 2019", 120, now=1.8e9)
    assert not qualify.is_stale("https://x/rfq", "no date here", 120, now=1.8e9)


def test_scoring_penalties_and_contact():
    a = assessment(page_type="ecommerce_listing", company_name="Shop", builds_own_product=False,
                   project_signal="none")  # fmt: skip
    shop = scoring.score(
        scoring.ScoreInput(a, analyze("https://s.example", "", SHOP_TEXT), 0, True, True)
    )
    assert shop.total == 0 and "page type: ecommerce_listing" in shop.penalties
    good = assessment(
        company_name="VoltGrid", industry="EV charging", engineering_needs=["embedded Linux"]
    )
    card = scoring.score(
        scoring.ScoreInput(good, analyze("https://voltgrid.com", "", EV_TEXT), 2, True, True)
    )
    cto = scoring.with_contact(
        card, Contact(name="Ana", role="CTO", email="ana@voltgrid.com", rank=0)
    )
    box = scoring.with_contact(card, Contact(email="sales@voltgrid.com", rank=10))
    assert cto.total - card.total == 10 and box.total - card.total == 6
    merged = scoring.merge(card, cto, sources=2)
    assert merged and merged.total >= cto.total


def test_model_reading_a_competitor_is_rejected():
    text = ("Acme Embedded builds Android and embedded Linux solutions for EV chargers and kiosks, on i.MX8 and "
            "Rockchip. Our services: BSP bring-up and drivers.")  # fmt: skip
    page = Page("https://acme-embedded.example/", "Acme Embedded", text)
    model = ScriptedModel({page.url: assessment(page_type="engineering_services_provider", company_name="Acme Embedded",
                                                 builds_own_product=False, project_signal="none")})  # fmt: skip
    v = _run(page, model)
    assert not v.accepted and "competitor" in v.reason


def test_foundations_and_universities_are_not_buyers():
    text = "LF Energy hosts open-source projects for EV charging (OCPP, embedded Linux on i.MX8, CAN bus, OTA updates)."
    page = Page("https://lfenergy.org/", "LF Energy", text)
    model = ScriptedModel({page.url: assessment(company_name="LF Energy")})
    assert "not a company" in _run(page, model).reason
    assert qualify.requirements(["AOSP", "embedded Linux"], ["aosp", "embedded_linux", "ota"]) == [
        "AOSP", "embedded Linux", "OTA updates"]  # fmt: skip
