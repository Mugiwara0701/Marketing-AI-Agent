"""Find a PUBLIC business contact on a company's own website. Never guesses or constructs addresses."""

import re

from agentkit.log import get_logger

from . import web
from .tasks import contact as contact_task

log = get_logger("agent.contacts")

# /impressum and /imprint are legally required in DACH countries and list a contact address.
_PATHS = ["", "/contact", "/contact-us", "/impressum", "/imprint", "/about", "/company", "/team"]
_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_USELESS = (
    "noreply",
    "no-reply",
    "donotreply",
    "abuse@",
    "privacy@",
    "dpo@",
    "webmaster@",
    "postmaster@",
    "press@",
    "unsubscribe",
)
_MAX_TEXT = 7000

# domain -> URL of a contact form seen during the last find_contact(domain). Read it when no email was found.
form_urls: dict[str, str] = {}


def emails_on_domain(text: str, domain: str) -> list[str]:
    found = {m.lower().rstrip(".") for m in _EMAIL.findall(text)}
    return sorted(
        e
        for e in found
        if e.split("@", 1)[1].endswith(domain) and not any(u in e for u in _USELESS)
    )


# Role mailboxes that companies publish for business enquiries, best first.
_ROLE_ORDER = (
    "sales",
    "business",
    "partner",
    "contact",
    "hello",
    "enquir",
    "inquir",
    "info",
    "office",
    "engineering",
)


def manual_reason(domain: str) -> str | None:
    """Why a company must be contacted by hand: its site refused automated visitors this run."""
    blocked = web.bot_blocked & {domain, f"www.{domain}"}
    return "site blocks automated access (bot check)" if blocked else None


def pick_role_address(candidates: list[str]) -> str | None:
    """Rule-based fallback when the model fails: the best business role address published on the site."""
    ranked = [
        (i, c)
        for c in candidates
        for i, k in enumerate(_ROLE_ORDER)
        if c.split("@", 1)[0].startswith(k)
    ]
    return min(ranked)[1] if ranked else None


async def _base(domain: str) -> str | None:
    """'https://domain' or, if that host does not answer, 'https://www.domain'."""
    for host in (domain, f"www.{domain}"):
        if host in web.bot_blocked:
            break
        if await web.fetch(f"https://{host}/") is not None:
            return f"https://{host}"
    return None


async def pick_contact(domain: str, pages: list[tuple[str, str]]):
    """From already-read pages (url, visible text) choose a public business contact on `domain`.
    Returns (ContactResult, source_url, problems) or None. Shared by the HTTP and the desktop pipelines."""
    for url, text in pages:
        candidates = emails_on_domain(text, domain)
        if not candidates:
            continue
        try:
            result, problems = await contact_task.extract_contact(text[:_MAX_TEXT])
        except Exception:
            log.info(
                "contact extraction failed, using rule fallback", extra={"ctx": {"domain": domain}}
            )
            result, problems = None, []
        email = result.email.strip().lower() if result else ""
        # Code-level guards on top of the model: literal on the page, on the company's own domain.
        if result and result.found and email in text.lower() and email in candidates:
            return result, url, problems
        fallback = pick_role_address(candidates)
        if fallback:
            res = contact_task.ContactResult(
                found=True,
                email=fallback,
                role="business contact",
                evidence=fallback,
                confidence=0.6,
            )
            return res, url, ["chosen by rule (role address on company site), not by the model"]
        log.info("contact rejected by guard", extra={"ctx": {"domain": domain}})
    return None


async def find_contact(domain: str):
    """Returns (ContactResult, source_url, problems) or None when no public business address is found."""
    form_urls.pop(domain, None)
    base = await _base(domain)
    if base is None:
        if domain in web.bot_blocked or f"www.{domain}" in web.bot_blocked:
            log.info(
                "site blocks automated access, contact it manually",
                extra={"ctx": {"domain": domain}},
            )
        return None
    pages: list[tuple[str, str]] = []
    seen_text: set[str] = set()
    for path in _PATHS:
        url = f"{base}{path}"
        body = await web.fetch_smart(url)
        text = web.html_to_text(body) if body else ""
        if body and domain not in form_urls and web.has_contact_form(body):
            form_urls[domain] = url
        if text and text not in seen_text:  # single-page sites answer every path with the same page
            seen_text.add(text)
            pages.append((url, text))
        if len(pages) >= 5:
            break
    return await pick_contact(domain, pages)
