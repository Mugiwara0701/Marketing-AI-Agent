"""Desktop backend: the visible Chrome on the Xubuntu desktop, used only through mouse, keyboard, clipboard and
screen (agent.gui.desktop.Desktop). It cannot see the DOM, so:

    search results  with SEARXNG_URL (normal case): the result links come from SearXNG (instant, no model call) and
                    the SearXNG results page is shown in Chrome so a person can watch. Without SearXNG: the query is
                    typed into an engine and the text model reads the results page (slow on a CPU)
    opening         a result is opened by typing its address into Chrome's address bar (Ctrl+L), never by hunting
                    for its title on screen or with the find bar
    links           Contact / Team / About links on a company site are clicked with the mouse by their visible text

A CAPTCHA or bot check is never worked around: the engine rests, the site is skipped.
"""

import asyncio
import contextlib
from urllib.parse import quote_plus, urlparse

from agentkit.config import env
from agentkit.llm import LLMError
from agentkit.log import get_logger

from ...gui.desktop import Desktop, DesktopError
from ...tasks import search as serp
from .. import intent
from ..models import Page, SearchResult
from . import Browser, Guard, SearchBlockedError

log = get_logger("agent.leadgen.browser.desktop")

# A result that turns out to be a search engine's own page is never a company's page.
SEARCH_ENGINES = frozenset({"google.com", "bing.com", "duckduckgo.com", "yahoo.com", "yandex.com", "ecosia.org",
                            "startpage.com", "brave.com", "baidu.com"})  # fmt: skip


def engine_target(engine: str, query: str) -> str:
    """What is typed into the address bar: the bare query for Chrome's own search, else the engine URL."""
    return query if engine == "default" else engine.format(q=quote_plus(query))


class DesktopBrowser:
    name = "desktop"

    def __init__(
        self, guard: Guard, engines: list[str], rest_minutes: float, desk: Desktop | None = None,
        lookup: Browser | None = None,
    ) -> None:  # fmt: skip
        self.guard = guard
        self.searx = (env("SEARXNG_URL") or "").rstrip("/")
        if lookup is None and self.searx:
            from .http import HttpBrowser  # noqa: PLC0415

            lookup = HttpBrowser(guard)
        self.lookup = lookup  # where result links come from (SearXNG); None = read the engine page with the model
        self.show_search = (env("DESKTOP_SHOW_SEARCH", "1") or "1") != "0"
        self.engines = engines or ["default"]
        self.rest_minutes = rest_minutes
        self.desk = desk or Desktop(run_id="leadgen")
        self._n = 0
        self.results_url = ""

    async def start(self) -> None:
        await self.desk.start()

    async def close(self) -> None:
        """End of the lead search, a dashboard Stop or a service shutdown: close the agent's Chrome (the next pass
        opens it again in seconds). DESKTOP_KEEP_CHROME=1 leaves it open, e.g. to look at a manual run."""
        if (env("DESKTOP_KEEP_CHROME", "0") or "0") == "1":
            return
        try:
            await self.desk.close_chrome()
        except (DesktopError, OSError):
            log.warning("could not close Chrome", exc_info=True)

    async def _form_on_screen(self) -> bool:
        """A form drawn on the screen. A long form shows its labels at the top and its button at the bottom, so the
        top and the bottom of the page are both read and judged together."""
        desk = self.desk
        seen = [await desk.ocr_text()]
        with contextlib.suppress(DesktopError):
            for where in ("top", "bottom"):
                await desk.scroll_to(where)
                seen.append(await desk.ocr_text())
            await desk.scroll_to("top")
        return intent.looks_like_screen_form("\n".join(seen))

    async def _read(self, label: str) -> Page:
        desk = self.desk
        title = await desk.title()
        # Before the copy: Ctrl+A highlights the whole page and OCR then reads it badly.
        on_screen = await self._form_on_screen() if intent.CONTACT_TITLE.search(title) else None
        text = await desk.read_page()
        if intent.is_consent_page(text) and await desk.dismiss_consent():
            text = await desk.read_page()
        await desk.shot(label)
        page = Page(
            url=await desk.current_url(), title=title, text=text
        )  # read the page first, address second
        if on_screen is None and intent.CONTACT_URL.search(page.url):
            on_screen = await self._form_on_screen()
        # a form in an iframe is not in the copied text: it is read from what is drawn on the screen
        page.has_contact_form = bool(on_screen) and not intent.looks_like_contact_form(text)
        page.blocked = intent.block_reason(text, title)
        if not page.blocked and desk.last_look and desk.last_look.blocked:
            page.blocked = "the vision model sees a CAPTCHA, login wall or access-denied page"
        return page

    async def search(self, query: str, limit: int) -> list[SearchResult]:
        if self.lookup is not None:
            results = await self.lookup.search(
                query, limit
            )  # SearchBlockedError goes to the pipeline
            if (
                self.show_search and self.searx
            ):  # let a person watch: the same search, in the visible window
                try:
                    await self.desk.navigate(f"{self.searx}/search?q={quote_plus(query)}")
                except DesktopError:
                    await self.desk.recover()
            return results
        return await self._search_engine_page(query, limit)

    async def _search_engine_page(self, query: str, limit: int) -> list[SearchResult]:
        for _ in range(len(self.engines)):
            engine = self.engines[self._n % len(self.engines)]
            self._n += 1
            if not self.guard.engine_ready(engine):
                continue
            try:
                await self.desk.navigate(engine_target(engine, query))
                page = await self._read(f"search {query}")
            except DesktopError as exc:
                await self.desk.recover()
                raise SearchBlockedError(f"desktop browser failed: {exc}") from exc
            if page.blocked:
                self.guard.rest_engine(engine, self.rest_minutes, page.blocked)
                continue
            self.results_url = page.url
            try:  # the text model reads the results page (the desktop cannot see links)
                read = await serp.read_results(page.text[:6000], query)
            except (LLMError, TypeError, ValueError) as exc:
                raise SearchBlockedError(
                    f"could not read the results page: {type(exc).__name__}"
                ) from exc
            return [
                SearchResult(h.title, "", h.snippet, engine, intent.registrable_domain(h.domain))
                for h in read.hits
            ][:limit]
        raise SearchBlockedError("every search engine is resting")

    async def _back_to_results(self) -> None:
        if self.results_url and await self.desk.current_url() != self.results_url:
            await self.desk.navigate(self.results_url)

    async def open(self, result: SearchResult) -> Page | None:
        if result.url:  # the address is known: type it, like a person pasting a link
            return await self.open_url(result.url)
        if result.domain and (why := self.guard.refuse(result.domain)):
            log.info("Result skipped", extra={"ctx": {"title": result.title[:80], "why": why}})
            return None
        await self._back_to_results()
        try:
            url = await self.desk.open_link(result.title, self.results_url)
        except DesktopError:
            await self.desk.recover()
            return None
        if not url:
            return None
        return await self._opened(url, f"page {result.title}")

    async def _opened(self, url: str, label: str) -> Page | None:
        dom = intent.registrable_domain(url)
        searx_host = urlparse(self.searx).hostname if self.searx else None
        if dom in SEARCH_ENGINES or (searx_host and urlparse(url).hostname == searx_host):
            log.info(
                "Not a result page (the window is on a search page)",
                extra={"ctx": {"url": url[:120]}},
            )
            return None
        if why := self.guard.refuse(url):
            log.info("Page skipped", extra={"ctx": {"url": url, "why": why}})
            return None
        self.guard.opened(url)
        page = await self._read(label)
        if (
            intent.registrable_domain(page.url) in SEARCH_ENGINES
        ):  # the window was not on the page we meant
            log.info(
                "Not a result page (the window is on a search page)",
                extra={"ctx": {"url": page.url[:120]}},
            )
            return None
        if page.blocked:
            self.guard.block(url, page.blocked)
        return page

    async def open_url(self, url: str) -> Page | None:
        if why := self.guard.refuse(url):
            log.info("Page skipped", extra={"ctx": {"url": url, "why": why}})
            return None
        try:
            await self.desk.navigate(url)
        except DesktopError as exc:
            log.warning("could not open page", extra={"ctx": {"url": url, "error": str(exc)[:200]}})
            await self.desk.recover()
            return None
        return await self._opened(
            url, f"open {url}"
        )  # the page is read first; its final address after

    async def follow(self, labels: list[str]) -> Page | None:
        # Only the mouse wheel and mouse clicks, like a person: the menu bar at the top first, then the footer. No
        # keyboard focus is needed, so nothing clicks the scrollbar (which would scroll the menu out of view).
        before = await self.desk.current_url(refocus=False)
        host = urlparse(before).hostname or ""
        for where in ("top", "bottom"):
            await self.desk.scroll_to(where)
            if await self.desk.click_link(*labels):
                break
        else:
            return None
        now = await self.desk.current_url(refocus=False)
        for _ in range(3):  # a slow page has not changed the address yet: give it a few seconds
            if now and now != before:
                break
            await asyncio.sleep(3)
            now = await self.desk.current_url(refocus=False)
        if not now or now == before:
            return None  # nothing opened (a chat widget, a dead link). No Back: it would leave the site for about:blank
        if intent.registrable_domain(now) != intent.registrable_domain(host):
            await (
                self.desk.back()
            )  # it led off the company's site: not what a contact link should do
            return None
        return await self._opened(now, f"link {labels[0]}")
