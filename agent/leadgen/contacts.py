"""Contact discovery: a legitimate business contact for a qualified company, from the company's OWN website.

    home page -> the Contact / Team / About links a person would click (only links that are on the page)
    -> addresses and people written on those pages -> the best one by role (decision makers first)

Never guessed: an address must be written on the company's site and belong to its domain; a person is kept only if
their name is on the page and their role is one we may approach. LinkedIn is never fetched; a profile link is kept
only when the company's own page links to it.
"""

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from agentkit.log import get_logger

from .. import contacts as rules
from ..tasks import contact as contact_task
from . import identity, intent
from .browser import Browser
from .config import LeadgenConfig
from .models import Contact, Page

log = get_logger("agent.leadgen.contacts")

# Role priority, best first (spec: decision maker > CTO > VP Eng > Head of Eng > Eng Manager > Technical Director >
# Founder > Co-founder > Project Manager > procurement / business development). Business mailboxes come after people.
ROLE_PRIORITY: tuple[tuple[int, str], ...] = (
    (0, r"\b(cto|chief technology officer|chief technical officer)\b"),
    (1, r"\b(vp|vice president)\b.{0,20}\b(engineering|r&d|technology)\b"),
    (
        2,
        r"\b(head|director) of (engineering|r&d|hardware|embedded|software|technology)\b|engineering director",
    ),
    (
        3,
        r"\bengineering manager\b|\b(embedded|firmware|software|hardware) (team )?(lead|manager)\b",
    ),
    (4, r"\btechnical director\b|\btechnology director\b|\bchief engineer\b"),
    (5, r"(?<!co-)(?<!co )\bfounder\b|\bceo\b|chief executive|managing director|\bowner\b"),
    (6, r"\bco-?founder\b"),
    (7, r"\b(project|program|product) manager\b"),
    (8, r"\b(procurement|purchasing|sourcing|business development|partnerships?|sales)\b"),
)
_ROLE_RX = [(rank, re.compile(p, re.I)) for rank, p in ROLE_PRIORITY]
_MAILBOX_RANK = {"engineering": 9, "business": 9, "partner": 9, "sales": 10, "contact": 11, "hello": 11,
                 "enquir": 11, "inquir": 11, "info": 12, "office": 12}  # fmt: skip
_ROLE_WORDS = re.compile(
    r"\b(cto|ceo|founder|vp|vice president|head of|director|manager|chief)\b", re.I
)


def role_rank(role: str) -> int | None:
    """Priority of a person's role (0 best), None when the role is not one we may approach."""
    for rank, rx in _ROLE_RX:
        if rx.search(role or ""):
            return rank
    return None


def mailbox_rank(address: str) -> int:
    local = address.split("@", 1)[0].lower()
    return next((r for k, r in _MAILBOX_RANK.items() if local.startswith(k)), 13)


@dataclass
class Discovery:
    contacts: list[Contact] = field(default_factory=list)  # best first
    pages: list[str] = field(default_factory=list)
    form_url: str | None = None
    blocked: str | None = None
    competitor: str | None = (
        None  # the home page shows an engineering-services firm: not a lead (why)
    )

    @property
    def best(self) -> Contact | None:
        return self.contacts[0] if self.contacts else None


def _literal(value: str, text: str) -> bool:
    return bool(value) and re.sub(r"\s+", " ", value.lower()) in re.sub(r"\s+", " ", text.lower())


FREE_MAIL = frozenset({
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.in", "yahoo.co.in", "outlook.com", "hotmail.com", "live.com",
    "msn.com", "rediffmail.com", "icloud.com", "me.com", "aol.com", "protonmail.com", "proton.me", "zohomail.com",
    "zohomail.in", "gmx.com", "mail.com", "yandex.com", "qq.com", "163.com", "126.com",
})  # fmt: skip
_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def company_free_mail(text: str, company: str) -> list[str]:
    """A Gmail / Yahoo / Outlook... address the company publishes as its own mailbox ("foogletech@gmail.com" for
    FoogleTech): on the company's own site AND named after the company. A person's free-mail address is not taken."""
    key = identity.name_key(company)
    if len(key) < 4:
        return []
    out = set()
    for addr in (m.lower().rstrip(".") for m in _EMAIL.findall(text)):
        local, _, host = addr.partition("@")
        local = re.sub(r"[^a-z0-9]", "", local)
        if host in FREE_MAIL and len(local) >= 4 and (key in local or local in key):
            out.add(addr)
    return sorted(out)


def candidates_from(
    page: Page, domain: str, people: contact_task.ContactResult | None, company: str = ""
) -> list[Contact]:
    """Code guards on top of the model: literal on the page, on the company's domain (or the company's own free-mail
    mailbox), an allowed role."""
    text = f"{page.text} " + " ".join(
        link.url[7:] for link in page.links if link.url.startswith("mailto:")
    )
    on_domain = set(rules.emails_on_domain(text, domain))
    linked = {
        link.url.split("?")[0].rstrip("/") for link in page.links if "linkedin.com/" in link.url
    }
    out: list[Contact] = []
    for p in people.people if people else []:
        rank = role_rank(p.role)
        if rank is None or not _literal(p.name, page.text) or not _literal(p.role, page.text):
            continue
        email = p.email.strip().lower()
        if email and email not in on_domain:
            email = ""  # not literally on the page, or not the company's own domain: never used
        li = p.linkedin.split("?")[0].rstrip("/")
        if li not in linked:
            li = ""
        if email:
            out.append(Contact(name=p.name.strip(), role=p.role.strip(), email=email, linkedin=li,
                               source=page.url, confidence=0.85, rank=rank))  # fmt: skip
    for addr in sorted(on_domain):
        if any(c.email == addr for c in out):
            continue
        rank = mailbox_rank(addr)
        if (
            rank < 13
        ):  # a business mailbox; personal-looking addresses without a name and role are not used
            out.append(
                Contact(
                    role="business contact", email=addr, source=page.url, confidence=0.6, rank=rank
                )
            )
    for addr in company_free_mail(text, company):
        if not any(c.email == addr for c in out):
            out.append(Contact(role="business contact (free-mail address on the company site)", email=addr,
                               source=page.url, confidence=0.5, rank=12))  # fmt: skip
    return out


Extractor = Callable[[str, str], Awaitable[contact_task.ContactResult]]


async def _people(page: Page, extract: Extractor) -> contact_task.ContactResult | None:
    """The model reads the page only when it names people with a role or shows addresses (saves model calls)."""
    if not (_ROLE_WORDS.search(page.text) or "@" in page.text):
        return None
    links = "\n".join(
        f"{link.text} {link.url}" for link in page.links if "linkedin.com/" in link.url
    )[:2000]
    try:
        return await extract(page.text[:7000], links)
    except Exception as exc:
        log.warning(
            "contact extraction failed, using rules only",
            extra={"ctx": {"url": page.url, "error": str(exc)[:120]}},
        )
        return None


def competitor(home: Page, cfg: LeadgenConfig) -> str | None:
    """Why the company is a competitor, judged on its home page: an engineering-services firm that asks for nothing.
    None when service companies are accepted as partner leads, or the home page is not one."""
    if cfg.q("accept_service_companies", False):
        return None
    hi = intent.analyze(home.url, home.title, home.text)
    if (hi.provider >= 3 or intent.SERVICES_TITLE.search(home.title)) and not hi.asks:
        return f"home page shows an engineering-services company ({home.title[:80]})"
    return None


async def discover(
    browser: Browser,
    domain: str,
    cfg: LeadgenConfig,
    extract: Extractor = contact_task.extract_contacts,
    company: str = "",
) -> Discovery:
    found = Discovery()
    if not identity.is_company_site(domain):
        found.blocked = f"{domain} is not a company website"
        return found
    home = await browser.open_url(f"https://{domain}")
    if home is None or home.blocked:
        found.blocked = home.blocked if home else "home page not reachable"
        log.info(
            "Contact discovery stopped", extra={"ctx": {"domain": domain, "why": found.blocked}}
        )
        return found
    if why := competitor(home, cfg):
        found.competitor = why
        log.info("Competitor found on the home page", extra={"ctx": {"domain": domain, "why": why}})
        return found
    pages = [home]
    max_pages = int(cfg.contacts.get("max_pages", 4))
    for labels in cfg.contacts.get("link_labels") or []:
        if len(pages) >= max_pages:
            break
        page = await browser.follow(list(labels))
        if page is None:
            continue
        if page.blocked:
            found.blocked = page.blocked
            break
        if all(page.url != p.url for p in pages):
            pages.append(page)
    found.pages = [p.url for p in pages]
    cands: list[Contact] = []
    for page in pages:
        if (
            page.has_contact_form or intent.looks_like_contact_form(page.text)
        ) and not found.form_url:
            found.form_url = page.url
        cands += candidates_from(page, domain, await _people(page, extract), company)
    best: dict[str, Contact] = {}
    for c in cands:
        if c.email not in best or (c.rank, -c.confidence) < (
            best[c.email].rank,
            -best[c.email].confidence,
        ):
            best[c.email] = c
    found.contacts = sorted(best.values(), key=lambda c: (c.rank, -c.confidence))
    log.info(
        "Contact search done",
        extra={"ctx": {"domain": domain, "pages": len(pages), "contacts": len(found.contacts),
                       "best_rank": found.best.rank if found.best else None}},
    )  # fmt: skip
    return found
