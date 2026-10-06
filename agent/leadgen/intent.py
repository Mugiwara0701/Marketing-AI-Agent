"""What a page IS, from its text and address alone (no model): a shop, a distributor, a job board, docs, news,
a project request, a product company... and how technically relevant it is.

This is the cheap first judge. It rejects the obvious non-leads (product listings, marketplaces, tutorials) before
any model call, and its counts feed the score. Domains are only one signal among several: a page is judged by what
it says, so an unknown shop is caught by its "add to cart" and prices, and a known site is not rejected for its
name alone unless the text agrees.
"""

import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

# --- vocabulary --------------------------------------------------------------------------------------------

TECH: dict[str, str] = {  # category -> pattern; a category counts once per page
    "aosp": r"\baosp\b|android open source project|android (platform|framework|bsp|porting|customi[sz]ation|"
    r"bring[- ]?up|os\b)|custom android|embedded android|android automotive|\baaos\b|android-based",
    "bsp": r"\bbsp\b|board support package|board bring[- ]?up|bring[- ]?up",
    "hal": r"\bhal\b|hardware abstraction layer|\bhidl\b|\baidl\b",
    "kernel": r"linux kernel|kernel (driver|module|porting|development)|device drivers?|linux drivers?|"
    r"driver development",
    "yocto": r"\byocto\b|openembedded|\bbuildroot\b|\bbitbake\b",
    "embedded_linux": r"embedded linux|linux-based|runs? (on )?linux|custom linux|linux (os|distribution) for",
    "firmware": r"\bfirmware\b|\brtos\b|freertos|zephyr rtos|bare[- ]metal",
    "bootloader": r"u-?boot|bootloader|device[- ]?tree",
    "interfaces": r"\buart\b|\bi2c\b|\bi²c\b|\bspi\b|can[- ]?bus|\bcan-fd\b|\bj1939\b|\bgpio\b|\bmipi\b",
    "ota": r"\bota\b|over[- ]the[- ]air",
    "soc": r"i\.?mx ?\d|rockchip|\brk3\d{3}\b|qualcomm|snapdragon|mediatek|\bnxp\b|sitara|stm32|allwinner|"
    r"amlogic|jetson|raspberry pi|\barm64\b|aarch64|cortex-a\d*",
}
DOMAINS: dict[str, str] = {
    "ev_charging": r"\bev charg(ing|ers?)\b|\b(dc|ac|fast|ev) chargers?\b|charging (station|point|dock|pile)s?|"
    r"charge points?|\bevse\b|\bocpp\b",
    "kiosk_signage": r"\bkiosks?\b|digital signage|self[- ]service terminal|vending machines?",
    "industrial": r"industrial automation|\bplc\b|\bhmi\b|industrial (gateway|iot|pc|controller)",
    "robotics": r"\brobot(s|ics)?\b|\bdrones?\b|\buavs?\b",
    "medical": r"medical devices?|patient monitor|diagnostic devices?",
    "automotive": r"infotainment|automotive|in-vehicle|telematics|\bfleet\b|\becus?\b",
    "consumer_devices": r"smart displays?|smart home|wearables?|set-top box|smart (tv|speaker)",
    "payments_retail": r"\bpos\b|point of sale|payment terminals?|card readers?|\brfid\b|\bnfc\b|barcode",
    "iot_edge": r"\biot\b|edge (computing|devices?|gateway)|connected devices?|\bgateways?\b|smart meters?",
    "handheld": r"handheld|rugged (tablet|device|computer)s?|industrial tablets?",
    "access_control": r"access control|biometric|face recognition terminal|time attendance",
}
TECH_LABELS = {
    "aosp": "AOSP / Android platform", "bsp": "BSP / board bring-up", "hal": "HAL", "kernel": "Linux kernel / drivers",
    "yocto": "Yocto / Buildroot", "embedded_linux": "embedded Linux", "firmware": "firmware", "bootloader":
    "bootloader / device tree", "interfaces": "UART / I2C / SPI / CAN / GPIO", "ota": "OTA updates", "soc": "ARM SoC",
}  # fmt: skip
_TECH = {k: re.compile(v, re.I) for k, v in TECH.items()}
# Organisations that do not buy engineering services the way a company does.
NOT_A_COMPANY = re.compile(
    r"\b(foundation|university|universit[àa]|institute of technology|college|school|consortium|alliance|association|"
    r"society|ministry|government|council|open[- ]source project)\b|^lf ",
    re.I,
)
_DOMAINS = {k: re.compile(v, re.I) for k, v in DOMAINS.items()}

# A page that sells: each pattern counts once.
_ECOMMERCE = [re.compile(p, re.I) for p in (
    r"add to (cart|basket|bag)", r"\bbuy now\b", r"\bcheckout\b", r"shopping (cart|bag)|view cart|your cart",
    r"\bin stock\b|out of stock|availability:", r"free shipping|ships (in|within)|delivery (in|within) \d",
    r"\bsku\b|\bmpn\b|part (number|no\.?)\s*[:#]", r"\bqty\b|quantity:|minimum order|\bmoq\b",
    r"\bwholesale\b|bulk (price|order)|unit price|per (piece|unit)\b|/\s?piece",
    r"customer reviews|write a review|\d(\.\d)? out of 5 stars",
    r"get (latest|best) price|request (a )?callback|contact supplier|send inquiry",
    r"\bwishlist\b|compare products|sort by price|price (range|filter)", r"\bfor sale\b|\bon sale\b",
)]  # fmt: skip
_PRICE = re.compile(
    r"(₹|\brs\.?\s?|\binr\s?|\$|\busd\s?|€|\beur\s?|£)\s?\d[\d,]*(\.\d+)?(?!\s?(million|billion|bn|m\b))",
    re.I,
)
_DISTRIBUTOR = [re.compile(p, re.I) for p in (
    r"authori[sz]ed distributor", r"\bdistributors? (of|for)\b", r"\breseller\b", r"we (stock|distribute)\b",
    r"franchised", r"\btraders?\b|\bdealers?\b|\bexporters?\b", r"brands we (carry|offer)|shop by brand",
    r"(manufacturer|supplier)s?,? (and|&) (supplier|exporter|trader)s?",
)]  # fmt: skip
# Somebody ASKING for outside work. Strong on its own.
_ASK = [re.compile(p, re.I) for p in (
    r"looking for (an? )?(experienced )?(embedded |android |linux |firmware |aosp |bsp )?(developer|engineer|freelancer|"
    r"team|company|partner|vendor|agency|contractor|consultant|subcontractor)s?",
    r"\b(we )?need(ed)? (an? )?(experienced )?\w* ?(developer|engineer|expert|freelancer|contractor)s?\b",
    r"request for (proposal|quotation)|\brfp\b|\brfq\b|\btender\b|\beoi\b|expression of interest",
    r"statement of work", r"submit (a |your )?(proposal|bid|quote)|proposals? (due|deadline)|bids? (due|deadline)",
    r"\bbudget\b|\bmilestones?\b|hourly rate|fixed[- ]price", r"freelance (project|job|work)|short[- ]term project",
    r"\bcontract (role|position|project|basis)\b", r"seeking (a |an )?(partner|vendor|contractor|subcontractor)",
)]  # fmt: skip
# Partner / outsourcing words: a request on a buyer's page, but the everyday marketing of a services company.
_PARTNER_WORDS = [re.compile(p, re.I) for p in (
    r"\boutsourc", r"\bsubcontract", r"(development|engineering|implementation|technology) partner",
)]  # fmt: skip
# A company that SELLS engineering services: a competitor, not a buyer.
_PROVIDER = [re.compile(p, re.I) for p in (
    r"\bour (embedded |engineering |software |development |design |bsp |aosp )*services\b",
    r"\bwe (offer|provide|deliver) .{0,60}(services|development|engineering|solutions)",
    r"(engineering|development|design|software) services (company|provider|firm)|design house",
    r"\b(hire|dedicated) (embedded |android |linux )?(developers|engineers|team)\b", r"\boffshore\b|\bnearshore\b",
    r"staff augmentation", r"\bour clients\b", r"case stud(y|ies)", r"(get|request) a (free )?quote",
    r"talk to (our )?(experts?|engineers)", r"\byears of experience in\b", r"trusted (technology |engineering )?partner",
    r"(embedded|android|aosp|bsp|firmware|linux) (development|engineering) services",
    r"\bhire me\b", r"\bmy (services|portfolio|skills|experience)\b",
    r"\bi am an? (experienced |senior |certified |freelance )*(\w+ )?(developer|engineer|consultant|freelancer)\b",
)]  # fmt: skip
# A result title that sells development work ("Kiosk Software Development - SoftTeco"): a competitor. Job titles
# ("Embedded Software Development Engineer") are not caught.
SERVICES_TITLE = re.compile(
    r"\bdevelopment (company|companies|services|agency|partner)\b|"
    r"\b(software|app|application|firmware|embedded|iot|kiosk|automotive|product|solutions?)( (\w+|&)){0,2} "
    r"development\b(?! (engineer|kit|board|lead|manager))|"
    r"\bengineering services\b|\bsoftware house\b|\boutsourcing (company|partner)\b",
    re.I,
)
_FORM_FIELDS = re.compile(
    r"\b(full name|your name|first name|last name|(work |business |your )?e-?mail( address)?|phone( number)?|"
    r"company( name)?|subject|your message|message|tell us about|describe your project)\b\s*\*?",
    re.I,
)
_FORM_BUTTON = re.compile(
    r"\b(send message|send inquiry|send enquiry|submit|send|get in touch|request a call)\b", re.I
)


def looks_like_contact_form(text: str) -> bool:
    """A contact form, read from the page's visible text (the desktop browser has no HTML): several field labels and a
    send / submit button."""
    labels = {m.group(1).lower().split()[-1] for m in _FORM_FIELDS.finditer(text or "")}
    return len(labels) >= 3 and bool(_FORM_BUTTON.search(text or ""))


# A title of a list or directory of companies ("Top 66 EV charging startups"): it names many companies, it is none.
LIST_PAGE = re.compile(
    r"\b(top|best) \d+\b|\b\d+ (best|top|leading|biggest|largest)\b|"
    r"\b(top|best|leading|biggest|largest) (\w+ ){0,4}(companies|startups|manufacturers|vendors|providers|firms|"
    r"suppliers|apps|tools|brands|devices|products|models)\b|\blist of\b|\bdirectory\b|\bbusiness listings?\b|"
    r"\breviewed\b|\b(reviews?|comparison) (\d{4}|of)\b",
    re.I,
)
# A title or snippet of someone advertising themselves: a freelancer, a profile, a "hire developers" agency page.
SELF_PROMO = re.compile(
    r"\b(hire|top|best) (\d+ )?(freelance |remote |dedicated )?(\w+ ){0,2}(developers|engineers|programmers|experts)\b|"
    r"\bfreelance (\w+ ){0,2}(developer|engineer|consultant)s?\b|\b(developer|engineer)s? for hire\b|"
    r"\b(my|view my|download my) (profile|portfolio|resume|cv)\b|\bcurriculum vitae\b",
    re.I,
)
_PRODUCT_DEV = [re.compile(p, re.I) for p in (
    r"\bwe (design|develop|build|engineer|manufacture)\b", r"designed (and|&) (manufactured|built|developed)",
    r"\bour (product|device|platform|charger|kiosk|robot|controller|gateway|display|hardware|terminal)s?\b",
    r"\bin[- ]house\b", r"\br&d\b|research (and|&) development", r"\bprototype", r"next[- ]generation",
    r"\blaunch(ed|ing)?\b", r"our (engineering|hardware|firmware|software) team", r"\bproprietary\b",
)]  # fmt: skip
# Words of a company that makes physical devices, whatever its market. Its home page rarely names AOSP or Yocto, but
# a device with a screen, a board and software is exactly where our work goes.
_HARDWARE = [re.compile(p, re.I) for p in (
    r"\bdevices?\b", r"\bhardware\b", r"\bembedded\b", r"\b(control|main|carrier|custom) ?boards?\b|\bpcb\b",
    r"\bcontrollers?\b", r"\btouch ?screens?\b|\bdisplays?\b|\bhmi\b", r"\bsensors?\b", r"\bterminals?\b",
    r"\bandroid\b", r"\blinux\b", r"\bconnectivity\b|\b(4g|5g|lte|wi-?fi|bluetooth|ble)\b",
    r"\b(oem|odm)\b|\bmanufactur(e|er|ing)\b|\bour factory\b",
)]  # fmt: skip
_EMPLOYMENT = (
    "full-time", "full time", "part-time", "apply now", "apply for this job", "years of experience",
    "we are hiring", "we're hiring", "join our team", "internship", "job description", "key responsibilities",
    "qualifications", "salary", "benefits", "equal opportunity employer", "permanent position", "notice period",
    "job type", "easy apply", "send your resume", "send your cv", "upload your resume",
)  # fmt: skip
_DOCS = [re.compile(p, re.I) for p in (
    r"\btutorial\b", r"\bdocumentation\b", r"how to (build|install|flash|compile|configure)", r"step \d\b",
    r"getting started", r"api reference", r"\$ (sudo|git|make|bitbake|repo) ", r"table of contents",
    r"this (guide|article) (shows|explains|describes)",
)]  # fmt: skip
_FORUM = re.compile(r"\breplies\b|\bposted by\b|\bthread\b|\bupvote|\bjoined:|\bposts:\s?\d", re.I)
_NEWS = re.compile(
    r"press release|\bnewsroom\b|\bannounced (today|that)\b|for immediate release", re.I
)

# Hosts: one signal among several (see module doc).
MARKETPLACES = frozenset({
    "indiamart.com", "tradeindia.com", "amazon.com", "amazon.in", "alibaba.com", "aliexpress.com", "ebay.com",
    "flipkart.com", "made-in-china.com", "globalsources.com", "dhgate.com", "exportersindia.com", "justdial.com",
    "robu.in", "etsy.com", "walmart.com", "newegg.com", "banggood.com", "tindie.com", "1688.com",
})  # fmt: skip
DISTRIBUTOR_HOSTS = frozenset({
    "mouser.com", "digikey.com", "farnell.com", "element14.com", "arrow.com", "avnet.com", "rs-online.com",
    "newark.com", "lcsc.com", "tme.eu", "octopart.com", "findchips.com",
})  # fmt: skip
JOB_HOSTS = frozenset({
    "indeed.com", "naukri.com", "linkedin.com", "glassdoor.com", "glassdoor.co.in", "monster.com",
    "ziprecruiter.com", "foundit.in", "shine.com", "simplyhired.com", "dice.com", "remoteok.com",
    "weworkremotely.com", "remotive.com", "jooble.org", "adzuna.com", "talent.com", "careerjet.com",
    "jobrapido.com", "instahyre.com", "cutshort.io", "hirist.tech", "iimjobs.com",
})  # fmt: skip
ATS_HOSTS = frozenset({"greenhouse.io", "lever.co", "workable.com", "ashbyhq.com", "recruitee.com",
                       "personio.de", "personio.com", "smartrecruiters.com", "bamboohr.com"})  # fmt: skip
FREELANCE_HOSTS = frozenset({"upwork.com", "freelancer.com", "freelancer.in", "guru.com", "peopleperhour.com",
                             "fiverr.com", "toptal.com", "truelancer.com"})  # fmt: skip
DIRECTORY_HOSTS = frozenset({
    "clutch.co", "goodfirms.co", "crunchbase.com", "zoominfo.com", "f6s.com", "g2.com", "capterra.com",
    "yellowpages.com", "kompass.com", "europages.co.uk", "europages.com", "thomasnet.com", "opencorporates.com",
    "dnb.com", "tracxn.com", "owler.com", "ensun.io", "designrush.com", "sortlist.com", "manta.com",
})  # fmt: skip
DOCS_HOSTS = frozenset({
    "stackoverflow.com", "stackexchange.com", "readthedocs.io", "kernel.org", "elinux.org", "android.com",
    "source.android.com", "yoctoproject.org", "wikipedia.org", "github.com", "gitlab.com", "medium.com",
    "geeksforgeeks.org", "w3schools.com", "youtube.com", "quora.com",
})  # fmt: skip

_SECOND_LEVEL = {"co", "com", "org", "net", "gov", "ac"}


def registrable_domain(host_or_url: str) -> str:
    """'https://www.careers.acme.co.uk/x' -> 'acme.co.uk'. Empty string if not a host."""
    host = urlparse(host_or_url if "//" in host_or_url else f"//{host_or_url}").hostname or ""
    parts = host.lower().removeprefix("www.").split(".")
    if len(parts) < 2:
        return ""
    keep = 3 if len(parts) >= 3 and parts[-2] in _SECOND_LEVEL and len(parts[-1]) == 2 else 2
    return ".".join(parts[-keep:])


def host_in(url: str, hosts: frozenset[str]) -> bool:
    host = (
        (urlparse(url if "//" in url else f"//{url}").hostname or "").lower().removeprefix("www.")
    )
    dom = registrable_domain(host)
    # amazon.de, ebay.co.uk ...: the brand label counts whatever the country ending
    brand = dom.split(".")[0] if dom else ""
    return host in hosts or dom in hosts or any(h.split(".")[0] == brand for h in hosts if brand)


# --- blocked / consent pages (no content to judge) ----------------------------------------------------------

_BLOCK_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("captcha", re.compile(r"captcha|unusual traffic from your|not a robot|are you a robot", re.I)),
    ("bot check", re.compile(
        r"verifying you are human|verify you are human|just a moment\.\.\.|checking your browser|attention required|"
        r"performing security verification|enable javascript and cookies to continue|"
        r"security service to protect against", re.I)),
    ("access denied", re.compile(r"access denied|403 forbidden|you have been blocked|request blocked|error 1020", re.I)),
    ("rate limited", re.compile(r"too many requests|rate limit(ed)? exceeded|error 429", re.I)),
    ("login wall", re.compile(r"(sign in|log in|login) to (continue|view|see|read)|create an account to (view|see|continue)", re.I)),
)  # fmt: skip
_CONSENT = re.compile(
    r"before you continue|we use cookies|accept all|cookie (settings|preferences|policy)|manage consent",
    re.I,
)


def block_reason(text: str, title: str = "") -> str | None:
    """Why this page is not usable (CAPTCHA, bot check, denial, rate limit, login wall), or None.
    Only the top of a page counts, so an article that merely mentions 'captcha' is not flagged."""
    head = f"{title}\n{text[:1500]}"
    short = len(text) < 2500
    for name, pat in _BLOCK_PATTERNS:
        if pat.search(head) and (short or name in ("captcha", "bot check")):
            return name
    return None


def is_consent_page(text: str) -> bool:
    return len(text) < 3500 and bool(_CONSENT.search(text[:1200]))


# --- the analysis --------------------------------------------------------------------------------------------


def _count(patterns: list[re.Pattern[str]], text: str) -> int:
    return sum(1 for p in patterns if p.search(text))


def employment_hits(text: str) -> int:
    low = text.lower()
    return sum(1 for n in _EMPLOYMENT if n in low)


_JOB_URL = re.compile(r"/(jobs?|careers?|vacanc(y|ies)|positions?|openings?|apply)(/|$|\?|-)", re.I)


@dataclass
class PageIntent:
    url: str
    tech: dict[str, int] = field(default_factory=dict)  # category -> 1
    domains: dict[str, int] = field(default_factory=dict)
    ecommerce: int = 0
    prices: int = 0
    distributor: int = 0
    asks: int = 0  # explicit requests for outside work (looking for a developer, RFQ, budget...)
    provider: int = 0  # wording of a company that sells engineering services
    project_request: int = (
        0  # asks, plus partner/outsourcing words when the page is not a services company
    )
    product_dev: int = 0
    hardware: int = (
        0  # device-maker words (device, hardware, controller, touchscreen, Android, Linux...)
    )
    employment: int = 0
    docs: int = 0
    forum: bool = False
    news: bool = False
    host_kind: str = (
        ""  # marketplace | distributor | job_board | ats | freelance | directory | docs | ""
    )
    job_url: bool = False

    @property
    def tech_terms(self) -> list[str]:
        return sorted(self.tech)

    @property
    def relevance_terms(self) -> int:
        return len(self.tech) + len(self.domains)

    @property
    def selling(self) -> int:
        """How strongly the page sells things: shop features plus visible prices (capped)."""
        return self.ecommerce + min(self.prices, 3)

    def summary(self) -> dict:
        return {
            "tech": self.tech_terms, "domains": sorted(self.domains), "ecommerce": self.ecommerce,
            "prices": self.prices, "distributor": self.distributor, "asks": self.asks, "provider": self.provider,
            "project_request": self.project_request,
            "product_dev": self.product_dev, "hardware": self.hardware, "employment": self.employment, "docs": self.docs,
            "forum": self.forum, "news": self.news, "host_kind": self.host_kind,
        }  # fmt: skip


def analyze(url: str, title: str, text: str) -> PageIntent:
    body = f"{title}\n{text}"
    host_kind = ""
    for kind, hosts in (("marketplace", MARKETPLACES), ("distributor", DISTRIBUTOR_HOSTS),
                        ("job_board", JOB_HOSTS), ("ats", ATS_HOSTS), ("freelance", FREELANCE_HOSTS),
                        ("directory", DIRECTORY_HOSTS), ("docs", DOCS_HOSTS)):  # fmt: skip
        if host_in(url, hosts):
            host_kind = kind
            break
    asks = _count(_ASK, body)
    provider = _count(_PROVIDER, body)
    partner = _count(_PARTNER_WORDS, body) if provider < 2 else 0
    return PageIntent(
        url=url,
        asks=asks,
        provider=provider,
        tech={k: 1 for k, p in _TECH.items() if p.search(body)},
        domains={k: 1 for k, p in _DOMAINS.items() if p.search(body)},
        ecommerce=_count(_ECOMMERCE, body),
        prices=len(_PRICE.findall(body[:20000])),
        distributor=_count(_DISTRIBUTOR, body),
        project_request=asks + partner,
        product_dev=_count(_PRODUCT_DEV, body),
        hardware=_count(_HARDWARE, body),
        employment=employment_hits(body),
        docs=_count(_DOCS, body),
        forum=bool(_FORUM.search(body[:4000])) and len(_FORUM.findall(body)) >= 3,
        news=bool(_NEWS.search(body[:3000])),
        host_kind=host_kind,
        job_url=bool(_JOB_URL.search(urlparse(url).path or "")),
    )


def relevant(i: PageIntent, min_tech: int = 2, *, services: bool = False) -> bool:
    """Worth a model call: someone asks for outside work, or the page shows our technology, or it shows a company
    that builds devices (in one of our markets, or plainly a device maker). Most product companies never name AOSP or
    Yocto on their home page, so the technology is not required."""
    return bool(
        i.asks
        or len(i.tech) >= min_tech
        or (i.tech and i.domains)
        or (i.domains and (i.product_dev or i.hardware >= 2))
        or (i.product_dev and i.hardware >= 3)
        # an engineering-services company: one sign that it works in our area is enough
        or (services and i.provider >= 2 and bool(i.tech or i.domains or i.hardware >= 2))
    )


def prefilter(  # noqa: PLR0911 - one reason per rule
    intent: PageIntent, *, min_relevance: int = 2, accept_services: bool = False
) -> str | None:
    """Reject reason for a page that is plainly not a lead, without asking the model. None = let the model judge.

    Order matters: a project request (an RFQ, "looking for a developer") is never thrown away for living on a
    marketplace or a forum; that judgement is left to the model and the score."""
    asks = intent.project_request >= 2
    if not relevant(intent, min_relevance, services=accept_services):
        return (
            "not relevant: no request, too little of our technology "
            f"({len(intent.tech)} term(s), need {min_relevance}) and no sign of a company building devices"
        )
    if not accept_services and intent.provider >= 3 and intent.asks == 0:
        return f"engineering services provider: a competitor, not a buyer ({intent.provider} service phrases)"
    if intent.host_kind == "marketplace" and not asks:
        return "marketplace product listing"
    if intent.host_kind == "distributor" or (intent.distributor >= 2 and not asks):
        return "distributor / reseller page"
    if intent.ecommerce >= 1 and intent.selling >= 3 and not asks and intent.employment < 2:
        return f"ecommerce page selling products ({intent.ecommerce} shop features, {intent.prices} prices)"
    if intent.host_kind == "directory":
        return "directory listing"
    if intent.host_kind == "docs" and not asks:
        return "documentation / tutorial / code hosting page"
    if intent.docs >= 3 and not asks and intent.product_dev == 0:
        return "documentation / tutorial"
    return None


# --- exclusions (business rules from config) ----------------------------------------------------------------


def has_word(text: str, word: str) -> bool:
    return bool(
        re.search(rf"(?<![a-z0-9]){re.escape(word.lower())}(?![a-z0-9])", (text or "").lower())
    )


def exclusion_reason(
    text: str, host: str, name: str, terms: list[str], companies: list[str]
) -> str:
    """Why a page or company is never a lead: an excluded topic (autonomous driving...) in `text`, or an excluded
    company as the site name (nuro.ai -> nuro) or the company name. "" when nothing excludes it."""
    if hit := next((t for t in terms if has_word(text, t)), None):
        return f"excluded topic '{hit}'"
    label = (host or "").lower().removeprefix("www.").split(".")[0]
    words = re.sub(r"[^a-z0-9 ]", " ", (name or "").lower()).split()
    joined = " ".join(words)
    for c in (x.lower() for x in companies):
        if (
            (label and label in (c, c.replace(" ", "")))
            or joined == c
            or joined.startswith(c + " ")
        ):
            return f"excluded company '{c}'"
    return ""


def country_excluded(host: str, location: str, countries: list[str], tlds: list[str]) -> str:
    """Why a company is outside the countries we pitch: its site ending (.de, .co.uk...) or the location read from
    its page. "" when it is not excluded or the location is unknown."""
    h = (host or "").lower().strip(".")
    if t := next((t for t in tlds if h.endswith(t.lower())), None):
        return f"site ends in {t}"
    if c := next((c for c in countries if has_word(location or "", c)), None):
        return f"based in {c}"
    return ""
