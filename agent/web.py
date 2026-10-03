"""Polite public-web access: robots.txt, per-host delay, no private addresses, text extraction."""

import asyncio
import ipaddress
import re
import socket
import time
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx

from agentkit.config import env
from agentkit.log import get_logger

from . import chrome, settings

log = get_logger("agent.web")

_HOST_DELAY = 1.5
_MAX_BYTES = 800_000
_last_hit: dict[str, float] = {}
_robots: dict[str, RobotFileParser | None] = {}
bot_blocked: set[str] = set()  # hosts that showed a bot check this run: not tried again

# Aggregators and socials: never treated as "the company's own website".
NOT_COMPANY_SITES = {
    "linkedin.com", "indeed.com", "glassdoor.com", "naukri.com", "monster.com", "ziprecruiter.com",
    "facebook.com", "twitter.com", "x.com", "instagram.com", "youtube.com", "github.com",
    "wikipedia.org", "crunchbase.com", "reddit.com", "ycombinator.com", "news.ycombinator.com",
    "remoteok.com", "remotive.com", "weworkremotely.com", "duckduckgo.com", "google.com",
    "bing.com", "medium.com", "angel.co", "wellfound.com", "lever.co", "greenhouse.io",
    "workable.com", "ashbyhq.com", "freelancer.com", "upwork.com", "glassdoor.co.in",
}  # fmt: skip
# Portals whose terms forbid scraping (docs/job-portals.xlsx, section 4). Never fetched, whatever the source.
DO_NOT_SCRAPE = {
    "linkedin",
    "indeed",
    "naukri",
    "glassdoor",
    "monster",
    "foundit",
    "ziprecruiter",
    "shine",
}
_SECOND_LEVEL = {"co", "com", "org", "net", "gov", "ac"}


def registrable_domain(host_or_url: str) -> str:
    """'https://www.careers.acme.co.uk/x' -> 'acme.co.uk'. Empty string if not a host."""
    host = urlparse(host_or_url if "//" in host_or_url else f"//{host_or_url}").hostname or ""
    parts = host.lower().removeprefix("www.").split(".")
    if len(parts) < 2:
        return ""
    keep = 3 if len(parts) >= 3 and parts[-2] in _SECOND_LEVEL and len(parts[-1]) == 2 else 2
    return ".".join(parts[-keep:])


def blocked(url: str) -> bool:
    """True for portals we must not scrape (any country domain: indeed.com, in.indeed.com, indeed.co.uk ...)."""
    labels = (urlparse(url if "//" in url else f"//{url}").hostname or "").lower().split(".")
    return any(label in DO_NOT_SCRAPE for label in labels)


def is_company_site(domain: str) -> bool:
    return bool(domain) and domain not in NOT_COMPANY_SITES


def _public_host(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, UnicodeError):  # UnicodeError: a label over 63 characters or empty
        return False
    return all(ipaddress.ip_address(i[4][0]).is_global for i in infos)


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript", "svg"):
            self._skip += 1
        if tag == "a":  # keep mailto targets: contact pages often hide the address in the href
            for k, v in attrs:
                if k == "href" and v and v.lower().startswith("mailto:"):
                    self.parts.append(unescape(v[7:].split("?")[0]))

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "svg") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip and data.strip():
            self.parts.append(data.strip())


def html_to_text(html: str) -> str:
    p = _Text()
    try:
        p.feed(html)
    except Exception:
        log.warning("html parse error")
    return re.sub(r"\s+", " ", " ".join(p.parts)).strip()


async def _allowed(client: httpx.AsyncClient, url: str) -> bool:
    u = urlparse(url)
    base = f"{u.scheme}://{u.netloc}"
    if base not in _robots:
        rp = RobotFileParser()
        try:
            r = await client.get(f"{base}/robots.txt", timeout=10)
            if r.status_code in (401, 403):
                _robots[base] = None  # explicit block
                return False
            rp.parse(r.text.splitlines() if r.status_code == 200 else [])
        except httpx.HTTPError:
            rp.parse([])
        _robots[base] = rp
    cached = _robots[base]
    return cached is not None and cached.can_fetch(settings.load().user_agent, url)


async def fetch(  # noqa: PLR0911 - every refusal returns None
    url: str, *, check_robots: bool = True, accept: str = "text/html"
) -> str | None:
    """GET a public page and return its body text (None on any refusal/failure)."""
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname or blocked(url):
        return None
    if u.hostname in bot_blocked or not await asyncio.to_thread(_public_host, u.hostname):
        return None
    headers = {"User-Agent": settings.load().user_agent, "Accept": accept}
    async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=20) as c:
        if check_robots and not await _allowed(c, url):
            return None
        wait = _last_hit.get(u.hostname, 0) + _HOST_DELAY - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        _last_hit[u.hostname] = time.monotonic()
        if (
            accept == "text/html" and chrome.enabled()
        ):  # visible mode: open the page in the Chrome window
            html = await chrome.goto(url)
            return html[:_MAX_BYTES] if html else None
        try:
            r = await c.get(url)
        except httpx.HTTPError:
            return None
    if r.status_code != 200:
        if r.status_code in (403, 429, 503) and chrome.bot_check(r.text):
            bot_blocked.add(u.hostname)
        return None
    if chrome.bot_check(r.text):  # a 200 page that is only the site's bot check
        bot_blocked.add(u.hostname)
        return None
    return r.text[:_MAX_BYTES]


async def fetch_page_text(url: str) -> str | None:
    body = await fetch(url)
    return html_to_text(body) if body else None


_MIN_TEXT = 200  # a page with less visible text than this is probably rendered by JavaScript


async def fetch_rendered(url: str) -> str | None:
    """Load a public page in headless Chromium (for JavaScript sites). Same guards as fetch().
    Returns the rendered HTML, or None if refused, failed, or Playwright is not installed."""
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname or blocked(url):
        return None
    if not await asyncio.to_thread(_public_host, u.hostname):
        return None
    try:
        from playwright.async_api import async_playwright  # noqa: PLC0415 - optional dependency
    except ImportError:
        return None
    async with httpx.AsyncClient(
        headers={"User-Agent": settings.load().user_agent}, follow_redirects=True, timeout=20
    ) as c:
        if not await _allowed(c, url):
            return None
    wait = _last_hit.get(u.hostname, 0) + _HOST_DELAY - time.monotonic()
    if wait > 0:
        await asyncio.sleep(wait)
    _last_hit[u.hostname] = time.monotonic()
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=not env("BROWSER_HEADED"))
            try:
                page = await browser.new_page(user_agent=settings.load().user_agent)
                await page.goto(url, wait_until="networkidle", timeout=25_000)
                return (await page.content())[:_MAX_BYTES]
            finally:
                await browser.close()
    except Exception:
        log.info("browser fetch failed", extra={"ctx": {"host": u.hostname}})
        return None


async def fetch_smart(url: str) -> str | None:
    """Plain HTTP first; if the page answers but has almost no text, retry in a real browser."""
    body = await fetch(url)
    if chrome.enabled():  # already rendered in the window
        return body
    if body is not None and len(html_to_text(body)) < _MIN_TEXT:
        return await fetch_rendered(url) or body
    return body


class _Forms(HTMLParser):
    """Finds a contact-style form: has a free-text message box, no password, not a search box."""

    def __init__(self) -> None:
        super().__init__()
        self.found = False
        self._cur: dict | None = None

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "").lower() for k, v in attrs}
        if tag == "form":
            self._cur = {"textarea": False, "password": False, "search": False}
            if a.get("role") == "search" or "search" in a.get("class", "") + a.get("id", ""):
                self._cur["search"] = True
        elif self._cur is not None:
            if tag == "textarea":
                self._cur["textarea"] = True
            elif tag == "input" and a.get("type") == "password":
                self._cur["password"] = True

    def handle_endtag(self, tag):
        c = self._cur
        if tag == "form" and c is not None:
            if c["textarea"] and not c["password"] and not c["search"]:
                self.found = True
            self._cur = None


def has_contact_form(html: str) -> bool:
    p = _Forms()
    try:
        p.feed(html)
    except Exception:
        log.warning("html parse error")
    return p.found
