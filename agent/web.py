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

from agentkit.log import get_logger

from . import settings

log = get_logger("agent.web")

_HOST_DELAY = 1.5
_MAX_BYTES = 800_000
_last_hit: dict[str, float] = {}
_robots: dict[str, RobotFileParser | None] = {}

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
    except socket.gaierror:
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


async def fetch(url: str, *, check_robots: bool = True, accept: str = "text/html") -> str | None:
    """GET a public page and return its body text (None on any refusal/failure)."""
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname or blocked(url):
        return None
    if not await asyncio.to_thread(_public_host, u.hostname):
        return None
    headers = {"User-Agent": settings.load().user_agent, "Accept": accept}
    async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=20) as c:
        if check_robots and not await _allowed(c, url):
            return None
        wait = _last_hit.get(u.hostname, 0) + _HOST_DELAY - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        _last_hit[u.hostname] = time.monotonic()
        try:
            r = await c.get(url)
        except httpx.HTTPError:
            return None
    if r.status_code != 200:
        return None
    return r.text[:_MAX_BYTES]


async def fetch_page_text(url: str) -> str | None:
    body = await fetch(url)
    return html_to_text(body) if body else None
