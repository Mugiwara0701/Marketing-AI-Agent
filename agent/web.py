"""Polite public-web access: robots.txt, per-host delay, no private addresses, text extraction."""

import asyncio
import ipaddress
import re
import socket
import time
from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx

from agentkit.config import env
from agentkit.log import get_logger

from . import settings

log = get_logger("agent.web")

_HOST_DELAY = float(
    env("HOST_DELAY_SECONDS", "2") or 2
)  # pause between two requests to the same host
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


_BOT_CHECK = re.compile(
    r"verifying you are human|just a moment\.\.\.|checking your browser|attention required|"
    r"security service to protect against malicious bots|enable javascript and cookies to continue|"
    r"verify you are human|performing security verification",
    re.I,
)


def bot_check(html: str) -> bool:
    """True if this is a site's bot-protection page (Cloudflare and similar), not the real content."""
    return bool(_BOT_CHECK.search(html[:6000])) and len(html) < 60_000


@dataclass
class Fetched:
    url: str  # final URL after redirects
    status: int
    html: str
    blocked: str | None = None  # "bot check" / "robots.txt" / "blocked portal" / "private address"


async def _wait_for_host(host: str) -> None:
    wait = _last_hit.get(host, 0) + _HOST_DELAY - time.monotonic()
    if wait > 0:
        await asyncio.sleep(wait)
    _last_hit[host] = time.monotonic()


async def fetch_page(  # noqa: PLR0911 - every refusal returns early
    url: str, *, check_robots: bool = True, accept: str = "text/html"
) -> Fetched | None:
    """GET a public page politely: robots.txt, per-host delay, no private addresses, no blocked portals, bot checks
    recognised (and the host remembered, never retried this run). None when the request itself failed."""
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        return None
    if blocked(url):
        return Fetched(url, 0, "", "portal whose terms forbid scraping")
    if u.hostname in bot_blocked:
        return Fetched(url, 0, "", "bot check (earlier this run)")
    if not await asyncio.to_thread(_public_host, u.hostname):
        return Fetched(url, 0, "", "not a public address")
    headers = {"User-Agent": settings.load().user_agent, "Accept": accept}
    async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=20) as c:
        if check_robots and not await _allowed(c, url):
            return Fetched(url, 0, "", "robots.txt disallows it")
        await _wait_for_host(u.hostname)
        try:
            r = await c.get(url)
        except httpx.HTTPError as exc:
            log.info(
                "fetch failed", extra={"ctx": {"host": u.hostname, "error": type(exc).__name__}}
            )
            return None
    final = str(r.url)
    if blocked(final):  # redirected onto a portal we must not read
        return Fetched(final, r.status_code, "", "redirected to a blocked portal")
    if bot_check(r.text):
        bot_blocked.add(u.hostname)
        log.info(
            "bot check, site skipped", extra={"ctx": {"host": u.hostname, "status": r.status_code}}
        )
        return Fetched(final, r.status_code, "", "bot check")
    return Fetched(final, r.status_code, r.text[:_MAX_BYTES])


async def fetch(url: str, *, check_robots: bool = True, accept: str = "text/html") -> str | None:
    """Body of a public page, or None on any refusal or failure (see fetch_page)."""
    got = await fetch_page(url, check_robots=check_robots, accept=accept)
    if got is None or got.blocked or got.status != 200:
        return None
    return got.html


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
    await _wait_for_host(u.hostname)
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=not env("BROWSER_HEADED"))
            try:
                page = await browser.new_page(user_agent=settings.load().user_agent)
                await page.goto(url, wait_until="networkidle", timeout=25_000)
                return (await page.content())[:_MAX_BYTES]
            finally:
                await browser.close()
    except Exception as exc:
        log.info(
            "browser fetch failed", extra={"ctx": {"host": u.hostname, "error": type(exc).__name__}}
        )
        return None


async def fetch_smart(url: str) -> str | None:
    """Plain HTTP first; if the page answers but has almost no text, retry in a real browser."""
    body = await fetch(url)
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


# A form that a script builds inside an iframe (HubSpot, Marketo, Typeform...): the page source holds only the embed.
EMBEDDED_FORM = re.compile(
    r"hs-form-iframe|hsforms\.(net|com)|hbspt\.forms\.create|mktoForm|pardot\.com|typeform\.com/to|jotform\.com|"
    r"tally\.so/(embed|r)|formspree\.io|docs\.google\.com/forms|forms\.office\.com|forms\.gle|cognitoforms\.com|"
    r"wufoo\.com|wpcf7|gform_wrapper|fluentform|ninja-forms|elementor-form",
    re.I,
)


def has_embedded_form(html: str) -> bool:
    return bool(EMBEDDED_FORM.search(html))


def has_contact_form(html: str) -> bool:
    p = _Forms()
    try:
        p.feed(html)
    except Exception:
        log.warning("html parse error")
    return p.found
