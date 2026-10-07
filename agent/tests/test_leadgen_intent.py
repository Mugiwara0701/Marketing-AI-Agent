"""Buyer vs seller: the rules that run before any model call."""

import pytest

from agent.leadgen import identity, intent

RPI_SHOP = (
    "Buy Raspberry Pi boards at the best price. Raspberry Pi 5 8GB Rs. 7,499 In stock. Add to cart. Buy now. "
    "Free shipping on orders above Rs. 999. Raspberry Pi 4 Rs. 4,999. Customer reviews 4.5 out of 5 stars. "
    "Embedded Linux ready, ARM64."
)
ANDROID_BOARD = (
    "Industrial Android development board available for ₹15,000. RK3568, Android 11, embedded Linux BSP included. "
    "Add to cart. Minimum order 1 piece. Get latest price. Ships within 3 days."
)
EV_CHARGER_SALE = (
    "EV charging controller board for sale. OCPP 1.6, CAN bus, embedded Linux. Price: $249 per piece. "
    "Wholesale price available. Contact supplier. In stock."
)
DISTRIBUTOR = (
    "Embedded development board supplier. Authorized distributor of NXP i.MX and Rockchip boards. "
    "We stock Jetson modules and Raspberry Pi. Brands we carry: NXP, TI, STM32. Embedded Linux BSP downloads."
)
EV_COMPANY = (
    "VoltGrid designs and manufactures DC fast chargers for fleets. Our charger runs embedded Linux on an i.MX8 "
    "controller board with CAN bus and UART links to the power modules. Our engineering team is developing the "
    "next-generation platform with OTA updates."
)
PROJECT_REQUEST = (
    "Looking for an engineer to customize AOSP for our custom ARM board. We need board bring-up, a HAL for our "
    "RFID reader and UART access. Fixed-price contract project; please submit your proposal with a budget."
)
TUTORIAL = (
    "How to build a Yocto image for Raspberry Pi. This tutorial explains each step. Step 1: clone poky. "
    "$ git clone poky. Step 2: run bitbake. Getting started with embedded Linux and the Linux kernel. "
    "Table of contents."
)


@pytest.mark.parametrize(
    ("url", "text", "why"),
    [
        ("https://pishop.example/rpi5", RPI_SHOP, "ecommerce"),
        ("https://boards.example/rk3568", ANDROID_BOARD, "ecommerce"),
        ("https://chargers.example/controller", EV_CHARGER_SALE, "ecommerce"),
        ("https://supply.example/", DISTRIBUTOR, "distributor"),
        ("https://www.indiamart.com/proddetail/ev-charger.html", EV_CHARGER_SALE, "marketplace"),
        ("https://docs.example/yocto", TUTORIAL, "documentation"),
    ],
)
def test_sellers_and_non_leads_are_rejected_without_the_model(url, text, why):
    reason = intent.prefilter(intent.analyze(url, "", text))
    assert reason is not None and why in reason


@pytest.mark.parametrize(
    ("url", "text"),
    [
        ("https://voltgrid.example/", EV_COMPANY),
        ("https://forum.example/t/123", PROJECT_REQUEST),
        (
            "https://www.indiamart.com/rfq/aosp",
            PROJECT_REQUEST,
        ),  # a request is never dropped for its site
    ],
)
def test_builders_and_requests_go_to_the_model(url, text):
    assert intent.prefilter(intent.analyze(url, "", text)) is None


def test_relevance_needs_our_stack_or_a_product_domain():
    pi = intent.analyze(
        "https://saas.example", "", "We build a CRM for sales teams with React and Node."
    )
    assert "not relevant" in (intent.prefilter(pi) or "")
    ev = intent.analyze("https://voltgrid.example", "", EV_COMPANY)
    assert {"embedded_linux", "soc", "interfaces", "ota"} <= set(ev.tech)
    assert "ev_charging" in ev.domains and ev.product_dev >= 2


def test_project_request_and_hiring_are_counted():
    assert intent.analyze("https://x.example", "", PROJECT_REQUEST).project_request >= 3
    job = (
        "Embedded Linux Engineer. Full-time. 5 years of experience. Apply now. Salary and benefits."
    )
    assert intent.analyze("https://x.example/careers/1", "", job).employment >= 3


def test_host_kinds_use_the_brand_whatever_the_country():
    assert intent.analyze("https://www.amazon.de/dp/1", "", "").host_kind == "marketplace"
    assert intent.analyze("https://boards.greenhouse.io/acme/jobs/1", "", "").host_kind == "ats"
    assert intent.analyze("https://acme-ev.com/", "", "").host_kind == ""


@pytest.mark.parametrize(
    ("text", "title", "reason"),
    [
        ("Verifying you are human. This may take a few seconds.", "Just a moment...", "bot check"),
        ("Our systems have detected unusual traffic from your computer network.", "", "captcha"),
        ("Sign in to continue to view this page", "", "login wall"),
        ("no available server", "boschrexroth.com/en/dc/", "server error"),
        ("502 Bad Gateway\nnginx", "502 Bad Gateway", "server error"),
        ("no healthy upstream", "", "server error"),
        (
            "We restore servers when no available server capacity is left. " * 60,
            "Hosting blog",
            None,
        ),
        ("VoltGrid builds chargers. " * 50, "VoltGrid", None),
    ],
)
def test_block_reason(text, title, reason):
    assert intent.block_reason(text, title) == reason


def test_exclusions_and_countries():
    assert "robotaxi" in intent.exclusion_reason(
        "we build a robotaxi fleet", "", "", ["robotaxi"], []
    )
    assert "tesla" in intent.exclusion_reason("", "tesla.com", "", [], ["tesla"])
    assert intent.exclusion_reason("", "", "Wind River Systems", [], ["wind river"])
    assert intent.country_excluded("acme.de", "", [], [".de"]) == "site ends in .de"
    assert (
        intent.country_excluded("acme.com", "Toronto, Canada", ["canada"], []) == "based in canada"
    )
    assert intent.country_excluded("acme.com", "", ["canada"], [".de"]) == ""


def test_identity_normalizes_names_and_urls():
    assert identity.name_key("Acme EV Technologies Pvt. Ltd.") == "acmeev"
    assert identity.name_key("ACME-EV Systems") == "acmeev"
    assert (
        identity.name_key("Technologies Ltd") == "technologies"
    )  # nothing distinctive: keep what is left
    a = identity.canonical_url("https://www.acme.com/about/?utm_source=x&b=1#team")
    assert a == identity.canonical_url("https://acme.com/about?b=1")
    assert identity.url_key("https://acme.com/a") == identity.url_key("https://www.acme.com/a/")


def test_company_identity_guards():
    assert identity.name_matches_domain("VoltGrid Energy", "voltgrid.com")
    assert identity.name_matches_domain("Advanced Battery Barn", "abb.com")  # initials
    assert not identity.name_matches_domain("VoltGrid Energy", "chargers-shop.com")
    assert not identity.is_company_site("indiamart.com") and not identity.is_company_site(
        "upwork.com"
    )
    assert identity.is_company_site("voltgrid.com")
    assert identity.name_on_page(
        "VoltGrid Energy", "welcome to voltgrid energy, makers of chargers"
    )
    assert not identity.name_on_page("Invented Corp", "welcome to voltgrid energy")


SERVICES_COMPANY = (
    "Multicore Technologies: your trusted partner for embedded development services. We offer AOSP customization, "
    "Android BSP and embedded Linux development for automotive and EV charging clients. Our services include board "
    "bring-up and device drivers. Hire dedicated embedded engineers. Read our case studies. Get a free quote."
)
SUBCONTRACT_REQUEST = (
    "We are an embedded design house with our own services, and we are looking for a subcontractor to take over "
    "Yocto BSP bring-up on an i.MX8 board for an EV charging client. Fixed-price; submit your proposal by Friday."
)


def test_engineering_services_companies_are_competitors_not_buyers():
    pi = intent.analyze("https://multicore.example/industries/ev", "", SERVICES_COMPANY)
    assert pi.provider >= 3 and pi.asks == 0
    assert (
        pi.project_request == 0
    )  # "partner" / "outsourcing" marketing is not a request on such a page
    assert "competitor" in (intent.prefilter(pi) or "")


def test_a_services_company_asking_for_a_subcontractor_is_still_a_lead():
    pi = intent.analyze("https://designhouse.example/rfq", "", SUBCONTRACT_REQUEST)
    assert pi.asks >= 2 and intent.prefilter(pi) is None


@pytest.mark.parametrize(
    "text",
    [
        "Freelance AOSP Developer | Android BSP expert available for remote work",
        "Hire Dedicated Embedded Linux Developers in 48 hours",
        "Top 10 Yocto developers for hire",
        "Ravi - Senior Android Engineer. View my portfolio and resume",
    ],
)
def test_people_and_agencies_advertising_themselves_are_spotted(text):
    assert intent.SELF_PROMO.search(text)


@pytest.mark.parametrize(
    "text",
    [
        "Kioskly company profile: we build self-service kiosks on RK3568",
        "Our product portfolio: DC fast chargers running embedded Linux",
        "VoltGrid is hiring an Embedded Linux Engineer for its charger platform",
        "RFQ: AOSP customization for our custom ARM board",
    ],
)
def test_companies_and_their_requests_are_not_mistaken_for_self_promotion(text):
    assert not intent.SELF_PROMO.search(text)


@pytest.mark.parametrize(
    ("title", "is_list"),
    [
        ("Top 66 Electric Vehicle Charging startups", True),
        ("Best EV Station App Development Companies (2026)", True),
        ("10 best kiosk manufacturers in India", True),
        ("EV Business Listings", True),
        ("Best Handheld Terminal Android Devices Reviewed for 2025 - Eff", True),
        ("Leading Manufacturer of RFID Readers & Tags | ID Tech", False),
        ("UHF RFID Reader Manufacturer in India | Identium", False),
        ("VoltGrid Energy - DC fast chargers for fleets", False),
        ("RFQ: AOSP customization for our custom board", False),
        ("Kioskly is hiring an Embedded Linux Engineer", False),
    ],
)
def test_list_pages(title, is_list):
    assert bool(intent.LIST_PAGE.search(title)) is is_list


@pytest.mark.parametrize(
    ("title", "is_services"),
    [
        ("Automotive App Development Company - Junkies Coder", True),
        ("EV Charging Solutions Development - Promwad", True),
        ("Kiosk Software Development - SoftTeco", True),
        ("Automotive Software and Electronics Development - Promwad", True),
        ("Embedded Software Development Engineer - VoltGrid careers", False),
        ("Linux Tablet & Panel PC Solutions | Industrial & Medical OEM", False),
        ("Custom POS, Kiosk & Retail Hardware | Ankh Innovations", False),
        ("i.MX8 development board", False),
    ],
)
def test_services_titles(title, is_services):
    assert bool(intent.SERVICES_TITLE.search(title)) is is_services
