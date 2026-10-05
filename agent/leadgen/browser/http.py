"""HTTP backend: SearXNG (SEARXNG_URL) or DuckDuckGo's HTML page for search, polite fetches for pages."""

import re
from html import unescape
from urllib.parse import parse_qs, urlparse

import httpx

from agentkit.config import env
from agentkit.log import get_logger

from ... import settings, web
from .. import extract, intent
from ..models import Page, SearchResult
from . import Guard, SearchBlockedError

log = get_logger("agent.leadgen.browser.http")


def parse_ddg(html: str) -> list[SearchResult]:
    """Results of DuckDuckGo's HTML endpoint."""
    out = []
    pat = r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>(?:.*?class="result__snippet"[^>]*>(.*?)</a>)?'
    for m in re.finditer(pat, html, re.S):
        href = unescape(m.group(1))
        if "uddg=" in href:
            href = parse_qs(urlparse(href).query).get("uddg", [""])[0]
        if href.startswith("//"):
            href = "https:" + href
        title = web.html_to_text(m.group(2))
        snippet = web.html_to_text(m.group(3) or "")
        out.append(
            SearchResult(title, href, snippet, "duckduckgo", intent.registrable_domain(href))
        )
    return out


class HttpBrowser:
    name = "http"

    def __init__(self, guard: Guard) -> None:
        self.guard = guard
        self.current: Page | None = None
        self.history: list[
            Page
        ] = []  # pages read this run, newest last (links are looked up here too)
        self._visited: set[str] = set()

    async def start(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def search(self, query: str, limit: int) -> list[SearchResult]:
        searx = env("SEARXNG_URL")
        engine = "searxng" if searx else "duckduckgo"
        if not self.guard.engine_ready(engine):
            raise SearchBlockedError(f"{engine} is resting after a refusal")
        headers = {"User-Agent": settings.load().user_agent}
        try:
            async with httpx.AsyncClient(headers=headers, timeout=30, follow_redirects=True) as c:
                if searx:
                    r = await c.get(
                        f"{searx.rstrip('/')}/search", params={"q": query, "format": "json"}
                    )
                else:
                    r = await c.post("https://html.duckduckgo.com/html/", data={"q": query})
        except httpx.HTTPError as exc:
            raise SearchBlockedError(f"{engine} unreachable: {type(exc).__name__}") from exc
        if r.status_code in (403, 429, 503) or web.bot_check(r.text):
            self.guard.rest_engine(engine, 30, f"HTTP {r.status_code}")
            raise SearchBlockedError(f"{engine} refused the query (HTTP {r.status_code})")
        if r.status_code != 200:
            raise SearchBlockedError(f"{engine} answered HTTP {r.status_code}")
        if searx:
            try:
                hits = r.json().get("results", [])
            except ValueError as exc:
                raise SearchBlockedError(
                    "searxng returned no JSON (is format=json enabled?)"
                ) from exc
            results = [
                SearchResult(h.get("title", ""), h["url"], h.get("content", ""), f"searxng:{h.get('engine', '')}",
                             intent.registrable_domain(h["url"]))
                for h in hits if isinstance(h, dict) and h.get("url")
            ]  # fmt: skip
        else:
            results = parse_ddg(r.text)
        return results[:limit]

    async def open_url(self, url: str) -> Page | None:
        if why := self.guard.refuse(url):
            log.info("Page skipped", extra={"ctx": {"url": url, "why": why}})
            return None
        got = await web.fetch_page(url)
        self.guard.opened(url)
        if got is None:
            return None
        if got.blocked:
            self.guard.block(got.url, got.blocked)
            return Page(url=got.url, title="", text="", blocked=got.blocked)
        if got.status != 200:
            log.info("Page not readable", extra={"ctx": {"url": url, "status": got.status}})
            return None
        page = extract.page_from_html(got.url, got.html)
        page.has_contact_form = web.has_contact_form(got.html)
        if (
            len(page.text) < 200
        ):  # a JavaScript-only page: try a real browser once, if Playwright is installed
            rendered = await web.fetch_rendered(got.url)
            if rendered:
                page = extract.page_from_html(got.url, rendered)
        if why := intent.block_reason(page.text, page.title):
            self.guard.block(got.url, why)
            page.blocked = why
        self.current = page
        self.history = [*self.history[-20:], page]
        self._visited.add(page.url)
        return page

    async def open(self, result: SearchResult) -> Page | None:
        return await self.open_url(result.url) if result.url else None

    async def follow(self, labels: list[str]) -> Page | None:
        """Click-equivalent: a link with one of these texts on the current page, else on an earlier page of the same
        site (a person going back to the home page and using its menu)."""
        if self.current is None:
            return None
        site = intent.registrable_domain(self.current.url)
        for page in [self.current, *reversed(self.history)]:
            if intent.registrable_domain(page.url) != site:
                continue
            if link := extract.pick_link(page, labels, exclude=self._visited):
                return await self.open_url(link.url)
        return None
