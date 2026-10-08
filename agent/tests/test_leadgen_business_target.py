"""Regression tests for the BUSINESS TARGET. They must hold for any technology a project brings.

Target: organisations that could BUY our embedded / AOSP / BSP / HAL / Linux / Yocto / firmware / application
engineering. Never: companies selling whatever technology a project uses, nor companies selling our services.

For every technology below the same four companies are built and must land the same way:
    component supplier                        -> rejected
    product builder using the technology      -> potential customer
    product builder + an engineering need     -> high-potential customer, scored above the plain builder
    company selling the engineering we sell   -> service company (competitor), rejected
and the strategy must search for customers using the technology as context, never for its sellers.

The model's reading is scripted here (what a correct reading says); everything after it is the real code.
"""

import asyncio
import inspect
import re

import pytest

from agent.leadgen import classify, intent, qualify, strategy
from agent.leadgen.classify import ResultType
from agent.leadgen.models import Page
from agent.tests.leadgen_fakes import ScriptedModel, assessment, cfg

# (technology, a component someone sells, a product someone builds with it)
TECHNOLOGIES = [
    ("RFID", "RFID reader", "access control system"),
    ("camera", "USB camera", "AI security kiosk"),
    ("display", "LCD panel", "industrial touchscreen terminal"),
    ("CAN", "CAN transceiver", "vehicle gateway"),
    ("NFC", "NFC module", "payment terminal"),
    ("GPS", "GPS module", "fleet telematics unit"),
    ("sensor", "temperature sensor", "industrial monitoring system"),
    ("EV", "EV charging connector", "EV charging station"),
    ("IoT", "Wi-Fi module", "smart home hub"),
    ("robotics", "servo motor", "warehouse robot"),
    ("automotive", "automotive relay", "infotainment head unit"),
    ("ARM", "ARM development board", "edge AI box"),
    ("Qualcomm", "Qualcomm system-on-module", "rugged handheld"),
    ("Rockchip", "Rockchip RK3568 board", "digital signage player"),
    ("NXP", "NXP i.MX8 module", "industrial HMI panel"),
    ("Raspberry Pi", "Raspberry Pi 5", "self-service kiosk"),
    ("Jetson", "Jetson Orin module", "machine vision system"),
    ("custom SoC", "SoC evaluation kit", "smart display"),
]
IDS = [t for t, _, _ in TECHNOLOGIES]
COMPONENT_WORDS = re.compile(
    r"\b(modules?|transceivers?|chipsets?|components?|evaluation kits?|development boards?|lcd panels?)\b",
    re.I,
)


def _co(tech: str) -> tuple[str, str]:
    slug = re.sub(r"[^a-z]", "", tech.lower())[:8]
    return f"Kestrel{slug.capitalize()} Systems", f"https://kestrel{slug}.example/"


def _run(page: Page, model: ScriptedModel, **conf):
    return asyncio.run(qualify.qualify_page(page, cfg(**conf), model))


# --- 1-4: the four companies, for every technology ----------------------------------------------------------


@pytest.mark.parametrize(("tech", "component", "product"), TECHNOLOGIES, ids=IDS)
def test_component_seller_is_rejected(tech, component, product):
    name, url = _co(tech)
    shop = Page(url, f"{component} | {name}", f"{name} sells {component} units. Buy {component} online. Price: $49 per "
                f"piece. In stock. Add to cart. Ships within 2 days. Datasheet download.")  # fmt: skip
    model = ScriptedModel({})
    v = _run(shop, model)
    assert not v.accepted and model.calls == []  # a shop never even reaches the model
    supplier = Page(url, name, f"{name} designs and supplies the {component} that other companies build into their "
                    f"{tech} products. Our {component} ships to product makers worldwide with embedded Linux drivers.")  # fmt: skip
    reading = assessment(page_type="company_product_page", company_name=name, company_role="sells_components_or_modules",
                         builds_own_product=False, project_signal="none")  # fmt: skip
    v = _run(supplier, ScriptedModel({url: reading}))
    assert (
        not v.accepted
        and v.classification
        and v.classification.type == ResultType.HARDWARE_SUPPLIER
    )


def _builder_page(tech: str, product: str, *, need: bool) -> Page:
    name, url = _co(tech)
    text = (f"{name} builds the {product}, a complete {tech} system running embedded Linux on an ARM board with a "
            f"touchscreen. Our engineering team develops the product in-house and launched it this year.")  # fmt: skip
    if need:
        text += f" We are hiring an Embedded Linux Engineer for BSP and device driver work on our {product}. Full-time."
    return Page(url, f"{product} | {name}", text)


def _builder_reading(tech: str, product: str, *, need: bool):
    name, _ = _co(tech)
    return assessment(
        page_type="hiring_post" if need else "company_product_page", company_name=name, product=product,
        company_role="builds_end_products", builds_own_product=True,
        project_signal="hiring" if need else "product_development",
        engineering_needs=["Linux BSP", "device drivers"] if need else [],
        evidence=[{"quote": "running embedded Linux on an ARM board", "reason": "the product runs embedded Linux"}],
    )  # fmt: skip


@pytest.mark.parametrize(("tech", "component", "product"), TECHNOLOGIES, ids=IDS)
def test_product_builder_is_a_potential_customer(tech, component, product):
    page = _builder_page(tech, product, need=False)
    v = _run(page, ScriptedModel({page.url: _builder_reading(tech, product, need=False)}))
    assert v.accepted, v.reason
    assert v.classification and v.classification.type == ResultType.POTENTIAL_CUSTOMER
    assert v.lead and v.lead.customer_tier == "potential"


@pytest.mark.parametrize(("tech", "component", "product"), TECHNOLOGIES, ids=IDS)
def test_product_builder_with_engineering_need_is_high_potential_and_scores_higher(
    tech, component, product
):
    plain = _builder_page(tech, product, need=False)
    needy = _builder_page(tech, product, need=True)
    v0 = _run(plain, ScriptedModel({plain.url: _builder_reading(tech, product, need=False)}))
    v1 = _run(needy, ScriptedModel({needy.url: _builder_reading(tech, product, need=True)}))
    assert v1.accepted and v1.lead and v1.lead.customer_tier == "high"
    assert v0.lead and v1.lead.lead_score > v0.lead.lead_score


@pytest.mark.parametrize(("tech", "component", "product"), TECHNOLOGIES, ids=IDS)
def test_company_selling_our_services_is_a_competitor(tech, component, product):
    name, url = _co(tech)
    page = Page(url, f"{name} - Embedded Development Services", f"{name} provides AOSP, Android BSP and embedded Linux "
                f"development services for {tech} products such as the {product}. Our services include board "
                f"bring-up and drivers. Hire our engineers. Our clients include product companies. Get a free quote.")  # fmt: skip
    model = ScriptedModel({})
    v = _run(page, model)
    assert not v.accepted and "competitor" in v.reason and model.calls == []
    reading = assessment(page_type="engineering_services_provider", company_name=name,
                         company_role="sells_engineering_services", builds_own_product=False, project_signal="none")  # fmt: skip
    kind = classify.classify(reading, intent.analyze(url, "", f"{name} AOSP services"))
    assert kind.type == ResultType.SERVICE_COMPANY and not kind.proceed


# --- semantic, not keyword: the same words mean different things in different company contexts --------------------


def test_a_maker_listing_the_parts_of_its_product_is_not_a_supplier():
    name, url = _co("RFID")
    page = Page(url, name, f"{name} builds the access control system, a complete product running embedded Linux on an ARM board. It "
                "uses an RFID reader, a camera and a touchscreen from our partners. Our team develops it in-house.")  # fmt: skip
    v = _run(
        page, ScriptedModel({url: _builder_reading("RFID", "access control system", need=False)})
    )
    assert (
        v.accepted and v.classification and v.classification.type == ResultType.POTENTIAL_CUSTOMER
    )


def test_a_service_company_asking_for_subcontractors_is_an_opportunity():
    reading = assessment(page_type="project_request", company_name="Kestrel", company_role="sells_engineering_services",
                         builds_own_product=False, project_signal="outsourcing_request")  # fmt: skip
    pi = intent.analyze("https://kestrel.example/rfq", "", "We are looking for a subcontractor for Yocto BSP work. "
                        "Submit your proposal.")  # fmt: skip
    kind = classify.classify(reading, pi)
    assert kind.type == ResultType.POTENTIAL_CUSTOMER and kind.tier == "high"


def test_hardware_maker_without_software_evidence_is_investigated_not_rejected_and_not_promoted():
    reading = assessment(company_name="Kestrel", company_role="builds_end_products", builds_own_product=True,
                         project_signal="product_development")  # fmt: skip
    pi = intent.analyze(
        "https://kestrel.example/", "", "Kestrel makes steel enclosures and cabinets for kiosks."
    )
    kind = classify.classify(reading, pi)
    assert (
        kind.type == ResultType.HARDWARE_MANUFACTURER
        and kind.proceed
        and kind.tier == "investigate"
    )


@pytest.mark.parametrize(
    ("page_type", "role", "expected"),
    [
        ("ecommerce_listing", "unknown", ResultType.ECOMMERCE),
        ("distributor_or_reseller", "resells_or_distributes", ResultType.RESELLER),
        ("news_article", "unknown", ResultType.INFORMATIONAL),
        ("documentation_or_tutorial", "unknown", ResultType.INFORMATIONAL),
        ("other", "unknown", ResultType.UNKNOWN),
    ],
)
def test_other_result_types(page_type, role, expected):
    reading = assessment(page_type=page_type, company_name="Kestrel", company_role=role, builds_own_product=False,
                         project_signal="none")  # fmt: skip
    kind = classify.classify(reading, intent.analyze("https://kestrel.example/", "", "text"))
    assert kind.type == expected and not kind.proceed


def test_job_and_freelance_posts_lead_to_the_company_behind_them():
    job = assessment(page_type="hiring_post", company_name="Kestrel", project_signal="hiring",
                     company_role="builds_end_products", engineering_needs=["Linux BSP"])  # fmt: skip
    assert (
        classify.classify(job, intent.analyze("https://jobs.example/1", "", "")).type
        == ResultType.JOB_POSTING
    )
    gig = assessment(
        page_type="project_request", company_name="", project_signal="outsourcing_request"
    )
    pi = intent.analyze(
        "https://www.upwork.com/jobs/aosp",
        "",
        "Looking for a developer to customize AOSP. Budget $5k.",
    )
    assert classify.classify(gig, pi).type == ResultType.FREELANCE_PROJECT


# --- the search side: customers, with technology as context -----------------------------------------------------


@pytest.mark.parametrize(("tech", "component", "product"), TECHNOLOGIES, ids=IDS)
def test_project_technology_is_context_never_the_search_target(tech, component, product):
    project = strategy.ProjectContext(products=(product,), technologies=(tech,))
    queries = list(strategy.generate(cfg(), project))[:60]
    assert queries and all(q.intent and q.intent.target == "potential_customer" for q in queries)
    for q in queries:
        low = q.text.lower()
        assert not strategy.SUPPLIER_WORDS.search(low), q.text
        assert component.lower() not in low, q.text  # never search for the component itself
        if tech.lower() in low and tech.lower() not in product.lower():
            assert f"{tech} {product}".lower() in low, q.text  # only as a modifier of the product
        assert any(
            s in low
            for s in (
                "company",
                "startup",
                "rfp",
                "rfq",
                "rfi",
                "tender",
                "proposal",
                "project",
                "quotation",
                "information",
                "procurement",
                "vendor selection",
                "statement of work",
            )
        ), q.text
    assert any(tech.lower() in q.text.lower() for q in queries)  # the context is used


def test_the_default_strategy_targets_products_never_components():
    c = cfg()
    assert all(not COMPONENT_WORDS.search(p) for p in c.search["products"])
    queries = list(strategy.generate(c))[:200]
    assert {q.family for q in queries} >= {"builder", "need", "hiring", "rfp", "direct"}
    assert "partner" not in {
        q.family for q in queries
    }  # service companies are not searched for by default
    assert (
        len({q.intent.product for q in queries[:12] if q.intent}) >= 10
    )  # a run covers many products


@pytest.mark.parametrize(
    "bad",
    [
        "RFID reader manufacturer",
        "camera module supplier",
        "buy Jetson Orin",
        "NXP i.MX8 OEM",
        "LCD panel price",
    ],
)
def test_a_supplier_query_is_refused(bad):
    with pytest.raises(strategy.RequirementDriftError):
        strategy.check_customer_query(bad)


def test_switching_projects_changes_queries_not_the_target():
    def product_queries(product: str, tech: str) -> set[str]:
        ctx = strategy.ProjectContext(products=(product,), technologies=(tech,))
        return {q.text for q in list(strategy.generate(cfg(), ctx))[:30] if q.family != "direct"}

    a, b = product_queries("vehicle gateway", "CAN"), product_queries("AI security kiosk", "camera")
    assert a and b and a.isdisjoint(b)  # "direct" queries name no product, so they may be shared
    ctx = strategy.ProjectContext(products=("AI security kiosk",), technologies=("camera",))
    assert all(
        q.intent and q.intent.target == "potential_customer" for q in strategy.generate(cfg(), ctx)
    )


# --- the code has no special case for any technology ----------------------------------------------------------


@pytest.mark.parametrize("fn", [classify.classify, qualify.qualify_page, qualify.company_problem, strategy.intents,
                                strategy.SearchIntent.query, intent.prefilter, intent.relevant])  # fmt: skip
def test_no_technology_is_special_cased_in_the_decision_code(fn):
    src = inspect.getsource(fn).lower()
    body = src.split('"""')[-1] if src.count('"""') >= 2 else src  # code after the docstring
    for tech in (
        "rfid",
        "camera",
        "nfc",
        "gps",
        "jetson",
        "raspberry",
        "qualcomm",
        "rockchip",
        "ev charg",
    ):
        assert tech not in body, f"{fn.__qualname__} mentions {tech!r}"
