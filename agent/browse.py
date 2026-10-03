"""Watchable lookup: a visible Chromium searches for a company, opens its site and contact pages, reads the contact.

One company at a time, slowed down so a person can follow it. Same rules as the rest of the agent: never a
blocked portal, never a private address, an email must be literally on the page and on the company's domain."""

import asyncio
import re
from urllib.parse import parse_qs, quote_plus, urlparse

from . import contacts, web
from .tasks import contact as contact_task

_SEARCH = "https://html.duckduckgo.com/html/?q="
_PAUSE = 1.5  # seconds between pages: polite to the site and watchable


async def _candidates(page, name: str) -> list[str]:
    """Company-looking domains from the first page of search results; ones matching the name come first."""
    await page.goto(
        f"{_SEARCH}{quote_plus(name + ' official website')}", wait_until="domcontentloaded"
    )
    await page.wait_for_timeout(2000)  # if a CAPTCHA shows up, it can be solved in the window
    hrefs = await page.eval_on_selector_all("a.result__a", "els => els.map(e => e.href)")
    seen: list[str] = []
    for h in hrefs:
        target = parse_qs(urlparse(h).query).get("uddg", [h])[0]  # DuckDuckGo wraps result links
        dom = web.registrable_domain(target)
        if dom and web.is_company_site(dom) and not web.blocked(target) and dom not in seen:
            seen.append(dom)
    tokens = [t for t in re.split(r"\W+", name.lower()) if len(t) >= 3][:2]
    return sorted(
        seen, key=lambda d: not any(t in d.split(".")[0] for t in tokens)
    )  # stable: matches first


async def lookup(query: str) -> str:
    """query: a company name or a domain. Returns a short report."""
    try:
        from playwright.async_api import async_playwright  # noqa: PLC0415 - optional dependency
    except ImportError:
        return "Playwright is not installed: pip install playwright && playwright install chromium"
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False, slow_mo=300)
        page = await browser.new_page()  # the search engine rejects a bot user agent
        try:
            if "." in query and " " not in query:
                domain = web.registrable_domain(query)
            else:
                found = await _candidates(page, query)
                print(f"search results: {', '.join(found[:5]) or 'none'}")  # noqa: T201
                domain = found[0] if found else ""
            if not domain or web.blocked(domain):
                return "no company website found"
            print(f"website: {domain}")  # noqa: T201
            await page.close()
            page = await browser.new_page(
                user_agent=web.settings.load().user_agent
            )  # sites see who we are
            report = await _read_site(page, domain)
            await page.wait_for_timeout(4000)  # leave the final page on screen for a moment
            return report
        finally:
            await browser.close()


async def _read_site(page, domain: str) -> str:
    form_url = ""
    for path in contacts._PATHS:
        url = f"https://{domain}{path}"
        if not await asyncio.to_thread(_ok, url):  # before loading: no private or blocked hosts
            return "refused: not a public company site"
        try:
            resp = await page.goto(url, wait_until="networkidle", timeout=25_000)
        except Exception:  # noqa: S112 - a page that times out is just skipped
            continue
        if resp is None or resp.status != 200:
            continue
        html = await page.content()
        text = web.html_to_text(html)
        if not form_url and web.has_contact_form(html):
            form_url = url
        candidates = contacts.emails_on_domain(text, domain)
        print(f"{url}: {len(text)} chars, emails: {candidates or 'none'}")  # noqa: T201
        if candidates:
            return await _choose(text, candidates, url, form_url)
        await asyncio.sleep(_PAUSE)
    if form_url:
        return f"no public email; contact form at {form_url}"
    return "no public contact found"


def _ok(url: str) -> bool:
    host = urlparse(url).hostname or ""
    return bool(host) and not web.blocked(url) and web._public_host(host)


async def _choose(text: str, candidates: list[str], url: str, form_url: str) -> str:
    try:
        result, problems = await contact_task.extract_contact(text[: contacts._MAX_TEXT])
    except Exception:
        result, problems = None, []
    email = result.email.strip().lower() if result else ""
    if result and result.found and email in text.lower() and email in candidates:
        return f"contact: {email} ({result.role or 'business contact'}) from {url}; checks: {problems or 'passed'}"
    fallback = contacts.pick_role_address(candidates)
    if fallback:
        return f"contact: {fallback} (chosen by rule, role address) from {url}"
    extra = f"; contact form at {form_url}" if form_url else ""
    return f"emails found but none passed the guard: {candidates}{extra}"
