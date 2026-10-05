"""Pure helpers for the desktop lead search: is this a project or a job post, how good is the lead, is the
page blocked, is the URL or query one we already saw. No I/O, so all of it is unit tested."""

import re
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

# Text that says "we are hiring a person". Two or more of these and no vendor wording means a job post.
_EMPLOYMENT = (
    "full-time", "full time", "part-time", "apply now", "apply for this job", "apply on", "years of experience",
    "we are hiring", "we're hiring", "join our team", "internship", "intern ", "job description", "key responsibilities",
    "qualifications", "salary", "benefits", "equal opportunity employer", "permanent position", "work from office",
    "notice period", "ctc", "job type", "easy apply", "send your resume", "send your cv", "upload your resume",
)  # fmt: skip
# Text that says "a company wants an outside team to deliver this".
_VENDOR = (
    "outsourc", "vendor", "agency", "rfp", "request for proposal", "statement of work", "development partner",
    "engineering partner", "external team", "looking for a partner", "looking for a company", "looking for an agency",
    "seeking a partner", "seeking a vendor", "turnkey", "fixed price", "fixed-price", "project-based",
    "project based", "dedicated team", "software house", "odm", "contract manufacturer", "request for quotation",
    "rfq", "tender", "proposals", "bids", "oem partner", "subcontract",
)  # fmt: skip
# Job boards and marketplaces where an individual engineer is hired: never a project lead for us.
INDIVIDUAL_HIRING_SITES = frozenset(
    {"upwork.com", "freelancer.com", "fiverr.com", "peopleperhour.com", "toptal.com", "guru.com",
     "greenhouse.io", "lever.co", "workable.com", "ashbyhq.com", "jobs.lever.co", "naukri.com",
     "indeed.com", "linkedin.com", "glassdoor.com", "monster.com", "ziprecruiter.com", "foundit.in",
     "remoteok.com", "remotive.com", "weworkremotely.com", "wellfound.com", "dice.com", "simplyhired.com"}
)  # fmt: skip
_JOB_URL = re.compile(r"/(jobs?|careers?|vacanc(y|ies)|positions?|openings?|apply)(/|$|\?)", re.I)

_BLOCK_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("captcha", re.compile(r"captcha|unusual traffic from your|not a robot|are you a robot|i'm not a robot", re.I)),
    ("cloudflare", re.compile(
        r"verifying you are human|just a moment\.\.\.|checking your browser|attention required|"
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
_TRACKING = ("utm_", "gclid", "fbclid", "mc_", "ref_src", "igshid", "sca_", "ved", "sxsrf")


def _count(text: str, needles: tuple[str, ...]) -> int:
    low = text.lower()
    return sum(1 for n in needles if n in low)


def vendor_hits(text: str) -> int:
    return _count(text, _VENDOR)


def employment_hits(text: str) -> int:
    return _count(text, _EMPLOYMENT)


def is_job_posting(text: str, url: str = "") -> bool:
    """True when the page is hiring a person rather than offering a project to a vendor."""
    host = urlparse(url).hostname or ""
    parts = host.lower().removeprefix("www.").split(".")
    domain = ".".join(parts[-2:])
    if (
        domain in INDIVIDUAL_HIRING_SITES
        or host.lower().removeprefix("www.") in INDIVIDUAL_HIRING_SITES
    ):
        return True
    vendor = vendor_hits(text)
    if vendor:
        return False
    jobs = employment_hits(text)
    return jobs >= 2 or (jobs >= 1 and bool(_JOB_URL.search(urlparse(url).path or "")))


def opportunity_type(text: str, url: str = "") -> str:
    low = text.lower()
    if any(
        k in low for k in ("rfp", "request for proposal", "request for quotation", "rfq", "tender")
    ):
        return "rfp"
    if any(
        k in low
        for k in (
            "outsourc",
            "vendor",
            "agency",
            "development partner",
            "engineering partner",
            "external team",
        )
    ):
        return "outsourcing"
    if any(
        k in low
        for k in (
            "contract",
            "freelance",
            "consultant",
            "project-based",
            "project based",
            "statement of work",
        )
    ):
        return "contract project"
    return "product company"


def block_reason(text: str, title: str = "") -> str | None:
    """Why this page is not usable (CAPTCHA, bot check, denial, rate limit, login wall), or None.
    Only the top of a page counts, so an article that merely mentions 'captcha' is not flagged."""
    head = f"{title}\n{text[:1500]}"
    short = len(text) < 2500
    for name, pat in _BLOCK_PATTERNS:
        if pat.search(head) and (short or name in ("captcha", "cloudflare")):
            return name
    return None


def is_consent_page(text: str) -> bool:
    return len(text) < 3500 and bool(_CONSENT.search(text[:1200]))


def canonical_url(url: str) -> str:
    """Same page, same string: no fragment, tracking parameters, 'www.' or trailing slash."""
    u = urlparse(url.strip())
    host = (u.hostname or "").lower().removeprefix("www.")
    query = urlencode(
        sorted((k, v) for k, v in parse_qsl(u.query) if not k.lower().startswith(_TRACKING))
    )
    path = u.path.rstrip("/") or ""
    return urlunparse((u.scheme.lower() or "https", host, path, "", query, ""))


def _tokens(s: str) -> set[str]:
    """Words of three or more letters, with a plural 's' dropped ('projects' ~ 'project')."""
    return {t.removesuffix("s") for t in re.split(r"\W+", s.lower()) if len(t) > 2}


def similar(a: str, b: str, threshold: float = 0.8) -> bool:
    """Token overlap (Jaccard) of two titles or queries; tolerates small wording changes."""
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return a.strip().lower() == b.strip().lower()
    return len(ta & tb) / len(ta | tb) >= threshold


def new_query(candidate: str, known: list[str]) -> bool:
    c = " ".join(candidate.split())
    return 8 <= len(c) <= 140 and not any(similar(c, k) for k in known)


def score_lead(
    *, confidence: float, vendor: int, keyword_hits: int, text_len: int, has_summary: bool,
    own_site: bool, contact: bool, product_hits: int = 0,
) -> tuple[int, dict[str, int]]:  # fmt: skip
    """0-100 qualification score from evidence, with the parts so a person can see why."""
    parts = {
        "model_confidence": round(max(0.0, min(confidence, 1.0)) * 40),
        "outsourcing_intent": min(vendor, 3) * 7,
        "technical_match": min(keyword_hits, 4) * 4,
        "product_match": min(product_hits, 3)
        * 7,  # words a device maker uses about itself (rfid, reader, pos...)
        "evidence": (4 if text_len >= 800 else 0) + (4 if has_summary else 0),
        "own_website": 5 if own_site else 0,
        "contactable": 10 if contact else 0,
    }
    return min(sum(parts.values()), 100), parts


def evidence_snippet(text: str, keywords: list[str], width: int = 160) -> str:
    """The text around the first keyword hit: what a person should read to check the lead."""
    low = text.lower()
    best = min((i for k in keywords if (i := low.find(k.lower())) >= 0), default=-1)
    if best < 0:
        return " ".join(text[: width * 2].split())
    return " ".join(text[max(0, best - width) : best + width].split())


def _has_word(text: str, word: str) -> bool:
    return bool(re.search(rf"(?<![a-z0-9]){re.escape(word.lower())}(?![a-z0-9])", text.lower()))


def exclusion_reason(
    text: str, host: str, name: str, terms: list[str], companies: list[str]
) -> str:
    """Why a result or page is never a lead: an excluded topic (autonomous driving...) in `text`, or an excluded
    company as the site name (nuro.ai -> nuro) or the company name. "" when nothing excludes it."""
    if hit := next((t for t in terms if _has_word(text, t)), None):
        return f"excluded topic '{hit}'"
    label = (host or "").lower().removeprefix("www.").split(".")[0]
    words = re.sub(r"[^a-z0-9 ]", " ", (name or "").lower()).split()
    for c in (x.lower() for x in companies):
        if label == c or (words and (words[0] == c or " ".join(words) == c)):
            return f"excluded company '{c}'"
    return ""


def query_on_topic(query: str, service: list[str], project: list[str], exclude: list[str]) -> bool:
    """A model-invented follow-up query is kept only if it names our kind of work AND a project/vendor need,
    and no excluded topic. With no lists configured every query passes."""
    if any(_has_word(query, t) for t in exclude):
        return False
    if service and not any(_has_word(query, t) for t in service):
        return False
    return not project or any(_has_word(query, t) for t in project)


def country_excluded(host: str, location: str, countries: list[str], tlds: list[str]) -> str:
    """Why a company is outside the countries we do not pitch: its site ends in an excluded domain (.de, .co.uk...)
    or the location read from its page names one. "" when it is not excluded or the location is unknown."""
    h = (host or "").lower().strip(".")
    if t := next((t for t in tlds if h.endswith(t.lower())), None):
        return f"site ends in {t}"
    if c := next((c for c in countries if _has_word(location or "", c)), None):
        return f"based in {c}"
    return ""
