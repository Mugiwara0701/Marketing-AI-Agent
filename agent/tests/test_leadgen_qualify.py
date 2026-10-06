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


def test_engineering_service_companies_are_partner_leads_or_competitors_by_config():
    text = ("Acme Embedded builds Android and embedded Linux solutions for EV chargers and kiosks, on i.MX8 and "
            "Rockchip. Our services: BSP bring-up and drivers. Our clients include automotive OEMs. Case studies.")  # fmt: skip
    page = Page("https://acme-embedded.example/", "Acme Embedded", text)
    reading = assessment(page_type="engineering_services_provider", company_name="Acme Embedded",
                         builds_own_product=False, project_signal="none",
                         evidence=[{"quote": "Our services: BSP bring-up and drivers", "reason": "embedded firm"}])  # fmt: skip
    v = _run(page, ScriptedModel({page.url: reading}), accept_service_companies=True)
    assert v.accepted, v.reason
    assert v.lead and v.lead.project_signal == "partner_capacity"
    assert not any("competitor" in k or "services" in k for k in v.lead.score.penalties)
    off = _run(page, ScriptedModel({page.url: reading.model_copy(update={"project_signal": "none"})}),
               accept_service_companies=False)  # fmt: skip
    assert not off.accepted and "competitor" in off.reason


def test_foundations_and_universities_are_not_buyers():
    text = "LF Energy hosts open-source projects for EV charging (OCPP, embedded Linux on i.MX8, CAN bus, OTA updates)."
    page = Page("https://lfenergy.org/", "LF Energy", text)
    model = ScriptedModel({page.url: assessment(company_name="LF Energy")})
    assert "not a company" in _run(page, model).reason
    assert qualify.requirements(["AOSP", "embedded Linux"], ["aosp", "embedded_linux", "ota"]) == [
        "AOSP", "embedded Linux", "OTA updates"]  # fmt: skip


KIOSK_HOME = (
    "Ankh Innovations designs and manufactures self-service kiosks and POS terminals for retail and restaurants. "
    "Our kiosks have 21-inch touchscreens, card readers, thermal printers and 4G connectivity. We build every "
    "device in-house, from the hardware to the ordering software. Contact our sales team."
)


def test_a_device_maker_that_never_names_our_stack_still_reaches_the_model_and_qualifies():
    page = Page("https://ankhinnovations.com/", "Ankh Innovations - Kiosks & POS", KIOSK_HOME)
    pi = analyze(page.url, page.title, page.text)
    assert (
        not pi.tech and pi.domains and pi.hardware >= 3
    )  # no AOSP / Yocto words, clearly a device maker
    model = ScriptedModel({page.url: assessment(
        company_name="Ankh Innovations", industry="retail kiosks", product="self-service kiosks and POS terminals",
        evidence=[{"quote": "We build every device in-house, from the hardware to the ordering software",
                   "reason": "builds its own devices"}],
    )})  # fmt: skip
    v = _run(page, model)
    assert model.calls == [page.url]
    assert v.accepted, v.reason
    assert v.lead and v.lead.score and v.lead.score.parts["technical_relevance"] >= 9


def test_pages_with_no_request_no_stack_and_no_device_maker_are_still_dropped():
    for url, text in [
        ("https://www.transportation.gov/rural/ev", "Partnership opportunities for EV charging in rural communities: "
         "funding programs, grants and planning toolkits for local governments."),
        ("https://news.example/ev", "EV sales grew 30% this quarter, analysts said on Monday."),
    ]:  # fmt: skip
        model = ScriptedModel({})
        v = _run(Page(url, "", text), model)
        assert not v.accepted and "not relevant" in v.reason and model.calls == []


IDTECH_HOME = (
    "Leading Manufacturer of RFID Readers & Tags | ID Tech Home Products Plastic Cards Smart Cards Card Printers "
    "Smart Card Readers RFID Readers Handheld Readers Biometric Devices Attendance and Access Control Devices "
    "Solutions RFID Parking Automation RFID Toll Automation. ID Tech designs and manufactures RFID readers, "
    "handheld terminals and biometric devices. Our clients include airports and hospitals."
)
FOOGLE_HOME = (
    "Embedded Systems & IoT Development Company | FoogleTech Software. Embedded & IoT Software Services: from "
    "bare-metal firmware and RTOS to embedded Linux, BSP and Yocto, we engineer complete embedded products for our "
    "clients. Hire Embedded Engineers. Outsource Embedded Dev. 150+ products shipped, 40+ global clients. "
    "Get a free consultation. Our services include Linux / BSP, PCB design and hardware."
)


def test_product_maker_is_not_penalised_for_client_wording_and_a_services_firm_is_rejected():
    idtech = Page(
        "https://idsolutionsindia.com/",
        "Leading Manufacturer of RFID Readers & Tags | ID Tech",
        IDTECH_HOME,
    )
    model = ScriptedModel({idtech.url: assessment(
        company_name="ID Tech", industry="RFID and identification", product="RFID readers, handheld terminals",
        evidence=[{"quote": "ID Tech designs and manufactures RFID readers", "reason": "makes its own devices"}],
    )})  # fmt: skip
    v = _run(idtech, model)
    assert v.accepted, v.reason
    assert (
        v.lead and v.lead.score and not v.lead.score.penalties
    )  # "our clients" does not count against a maker

    foogle = Page(
        "https://foogletech.com/", "Embedded Systems & IoT Development Company", FOOGLE_HOME
    )
    model = ScriptedModel({})
    v = _run(foogle, model)
    assert (
        not v.accepted and "competitor" in v.reason and model.calls == []
    )  # dropped without a model call
