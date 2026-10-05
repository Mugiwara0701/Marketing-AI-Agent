"""Chrome backend: a real Chrome (Playwright) with a persistent profile, one tab, one page at a time.

Search goes to CHROME_SEARCH_URL (default Bing); results are the external links on the results page, read from the
DOM. A CAPTCHA or bot check is never solved by the agent: the engine rests, the site is skipped and the run goes on
(CHROME_HUMAN_WAIT=<seconds> lets a person at the screen clear a check in the window instead).
"""

import asyncio
import contextlib
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

from agentkit.config import env
from agentkit.log import get_logger

from ... import web
from .. import extract, intent
from ..models import Link, Page, SearchResult
from . import Guard, SearchBlockedError

log = get_logger("agent.leadgen.browser.chrome")

_CONSENT_BUTTONS = re.compile(
    r"^(reject all|decline all|only necessary|necessary only|accept all|i agree|agree)$", re.I
)
_JS_LINKS = (
    "els => els.map(e => [e.href, (e.innerText || e.getAttribute('aria-label') || '').trim()])"
)


class ChromeBrowser:
    name = "chrome"

    def __init__(self, guard: Guard) -> None:
        self.guard = guard
        self.search_url = env("CHROME_SEARCH_URL", "https://www.bing.com/search?q={q}") or ""
        self.profile = Path(env("CHROME_PROFILE_DIR", ".chrome-profile") or ".chrome-profile")
        self.headless = (env("CHROME_HEADLESS", "0") or "0") == "1"
        self.human_wait = float(env("CHROME_HUMAN_WAIT", "0") or 0)
        self._pw: Any = None
        self._ctx: Any = None
        self._page: Any = None
        self._visited: set[str] = set()

    async def start(self) -> None:
        try:
            from playwright.async_api import async_playwright  # noqa: PLC0415 - optional dependency
        except ImportError as exc:
            raise RuntimeError(
                "BROWSER_BACKEND=chrome needs Playwright: pip install playwright"
            ) from exc
        self._pw = await async_playwright().start()
        kwargs = {"headless": self.headless, "viewport": None}
        try:
            self._ctx = await self._pw.chromium.launch_persistent_context(
                str(self.profile), channel="chrome", **kwargs
            )
        except Exception:
            log.info("Google Chrome not found, using Playwright's Chromium")
            self._ctx = await self._pw.chromium.launch_persistent_context(
                str(self.profile), **kwargs
            )
        self._page = self._ctx.pages[0] if self._ctx.pages else await self._ctx.new_page()
        log.info(
            "Browser initialized", extra={"ctx": {"backend": "chrome", "headless": self.headless}}
        )

    async def close(self) -> None:
        with contextlib.suppress(Exception):  # the window may already be closed by a person
            if self._ctx is not None:
                await self._ctx.close()
            if self._pw is not None:
                await self._pw.stop()
        self._pw = self._ctx = self._page = None

    async def _goto(self, url: str) -> tuple[int | None, str]:
        if self._page is None:
            await self.start()
        page = self._page
        resp = await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        with contextlib.suppress(Exception):  # some pages never go quiet; read them anyway
            await page.wait_for_load_state("networkidle", timeout=6_000)
        with contextlib.suppress(
            Exception
        ):  # cookie banner: refuse (or accept) so the content is visible
            await page.get_by_role("button", name=_CONSENT_BUTTONS).first.click(timeout=1500)
        return (resp.status if resp else None), page.url

    async def _snapshot(self) -> Page:
        page = self._page
        text = await page.evaluate("() => document.body ? document.body.innerText : ''")
        pairs = await page.eval_on_selector_all("a[href]", _JS_LINKS)
        links = [
            Link(" ".join(t.split())[:120], h)
            for h, t in pairs
            if h.startswith(("http", "mailto:"))
        ]
        form = await page.evaluate("() => !!document.querySelector('form textarea')")
        return Page(
            url=page.url,
            title=await page.title(),
            text=" ".join(text.split()),
            links=links,
            has_contact_form=bool(form),
        )

    async def _cleared(self, page: Page) -> Page:
        """A bot check: give a person CHROME_HUMAN_WAIT seconds to clear it in the window, else give up."""
        waited = 0.0
        while page.blocked and waited < self.human_wait:
            await asyncio.sleep(3)
            waited += 3
            page = await self._snapshot()
            page.blocked = intent.block_reason(page.text, page.title)
        return page

    async def search(self, query: str, limit: int) -> list[SearchResult]:
        engine = intent.registrable_domain(self.search_url) or "search"
        if not self.guard.engine_ready(engine):
            raise SearchBlockedError(f"{engine} is resting")
        try:
            await self._goto(self.search_url.format(q=quote_plus(query)))
            page = await self._snapshot()
        except Exception as exc:
            raise SearchBlockedError(f"{engine} failed: {type(exc).__name__}") from exc
        page.blocked = intent.block_reason(page.text, page.title)
        page = await self._cleared(page)
        if page.blocked:
            self.guard.rest_engine(engine, 30, page.blocked)
            raise SearchBlockedError(f"{engine}: {page.blocked}")
        out: list[SearchResult] = []
        seen: set[str] = set()
        for link in page.links:
            dom = intent.registrable_domain(link.url)
            if (
                not dom
                or dom == engine
                or dom in ("microsoft.com", "bing.net", "msn.com", "google.com", "duckduckgo.com")
            ):
                continue
            if len(link.text) < 12 or link.url in seen:
                continue
            seen.add(link.url)
            out.append(SearchResult(link.text, link.url, "", engine, dom))
        return out[:limit]

    async def open_url(self, url: str) -> Page | None:
        if why := self.guard.refuse(url):
            log.info("Page skipped", extra={"ctx": {"url": url, "why": why}})
            return None
        if web.blocked(url):
            return None
        self.guard.opened(url)
        try:
            status, final = await self._goto(url)
            page = await self._snapshot()
        except Exception as exc:
            log.info("Page failed", extra={"ctx": {"url": url, "error": type(exc).__name__}})
            if "closed" in str(exc).lower():
                await self.close()  # the window was closed: a new one opens on the next page
            return None
        page.blocked = intent.block_reason(page.text, page.title)
        page = await self._cleared(page)
        if page.blocked:
            self.guard.block(final, page.blocked)
        elif status and status >= 400:
            log.info("Page not readable", extra={"ctx": {"url": url, "status": status}})
            return None
        self._visited.add(page.url)
        return page

    async def open(self, result: SearchResult) -> Page | None:
        return await self.open_url(result.url) if result.url else None

    async def follow(self, labels: list[str]) -> Page | None:
        if self._page is None:
            return None
        link = extract.pick_link(await self._snapshot(), labels, exclude=self._visited)
        return await self.open_url(link.url) if link else None
