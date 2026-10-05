"""Desktop backend: the visible Chrome on the Xubuntu desktop, used only through mouse, keyboard, clipboard and
screen (agent.gui.desktop.Desktop). It cannot see the DOM, so:

    search results  the results page text is read by the LLM (task lead.search) into titles/domains/snippets
    opening         a result is clicked by its title (OCR, vision model as fallback)
    links           a link is clicked by its visible text; pages carry no link list

A CAPTCHA or bot check is never worked around: the engine rests, the site is skipped.
"""

from urllib.parse import quote_plus, urlparse

from agentkit.log import get_logger

from ...gui.desktop import Desktop, DesktopError
from ...tasks import search as serp
from .. import intent
from ..models import Page, SearchResult
from . import Guard, SearchBlockedError

log = get_logger("agent.leadgen.browser.desktop")


def engine_target(engine: str, query: str) -> str:
    """What is typed into the address bar: the bare query for Chrome's own search, else the engine URL."""
    return query if engine == "default" else engine.format(q=quote_plus(query))


class DesktopBrowser:
    name = "desktop"

    def __init__(
        self, guard: Guard, engines: list[str], rest_minutes: float, desk: Desktop | None = None
    ) -> None:
        self.guard = guard
        self.engines = engines or ["default"]
        self.rest_minutes = rest_minutes
        self.desk = desk or Desktop(run_id="leadgen")
        self._n = 0
        self.results_url = ""

    async def start(self) -> None:
        await self.desk.start()

    async def close(self) -> None:
        return None  # the window stays open on the desktop for the person to see

    async def _read(self, label: str) -> Page:
        desk = self.desk
        text = await desk.read_page()
        if intent.is_consent_page(text) and await desk.dismiss_consent():
            text = await desk.read_page()
        await desk.shot(label)
        title = await desk.title()
        page = Page(url=await desk.current_url(), title=title, text=text)
        page.blocked = intent.block_reason(text, title)
        if not page.blocked and desk.last_look and desk.last_look.blocked:
            page.blocked = "the vision model sees a CAPTCHA, login wall or access-denied page"
        return page

    async def search(self, query: str, limit: int) -> list[SearchResult]:
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
            read = await serp.read_results(
                page.text[:9000], query
            )  # the LLM reads the results page text
            return [
                SearchResult(h.title, "", h.snippet, engine, intent.registrable_domain(h.domain))
                for h in read.hits
            ][:limit]
        raise SearchBlockedError("every search engine is resting")

    async def _back_to_results(self) -> None:
        if self.results_url and await self.desk.current_url() != self.results_url:
            await self.desk.navigate(self.results_url)

    async def open(self, result: SearchResult) -> Page | None:
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
        if why := self.guard.refuse(url):
            log.info("Page skipped", extra={"ctx": {"url": url, "why": why}})
            return None
        self.guard.opened(url)
        page = await self._read(label)
        if page.blocked:
            self.guard.block(url, page.blocked)
        return page

    async def open_url(self, url: str) -> Page | None:
        if why := self.guard.refuse(url):
            log.info("Page skipped", extra={"ctx": {"url": url, "why": why}})
            return None
        try:
            await self.desk.navigate(url)
        except DesktopError:
            await self.desk.recover()
            return None
        return await self._opened(await self.desk.current_url() or url, f"open {url}")

    async def follow(self, labels: list[str]) -> Page | None:
        before = await self.desk.current_url()
        host = urlparse(before).hostname or ""
        for key in (
            "Home",
            "End",
        ):  # header links first, then the footer, like a person scrolling down
            await self.desk.act(action="key", key=key)
            if await self.desk.click_link(*labels):
                break
        else:
            return None
        now = await self.desk.current_url()
        if (
            not now
            or now == before
            or intent.registrable_domain(now) != intent.registrable_domain(host)
        ):
            await (
                self.desk.back()
            )  # it led off the company's site: not what a contact link should do
            return None
        return await self._opened(now, f"link {labels[0]}")
