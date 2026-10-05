"""Fakes for the lead pipeline tests: a scripted browser (a tiny fake web), a scripted model, a scripted drafter."""

from typing import Any

from agent.leadgen import config, extract
from agent.leadgen.browser import Guard, SearchBlockedError
from agent.leadgen.models import Page, SearchResult
from agent.tasks.assess import Assessment, Quote
from agent.tasks.proposal import EmailDraft


def cfg(**qual) -> config.LeadgenConfig:
    c = config.load("config/leadgen.yaml")
    c.qualification.update(qual)
    c.search.update({"queries_per_run": 3, "pages_per_query": 4, "query_suffix": ""})
    return c


def html(title: str, body: str, links: list[tuple[str, str]] = ()) -> str:  # type: ignore[assignment]
    a = "".join(f'<a href="{u}">{t}</a> ' for t, u in links)
    return (
        f"<html><head><title>{title}</title></head><body><nav>{a}</nav><p>{body}</p></body></html>"
    )


class FakeBrowser:
    """A fake web: url -> html, and canned search results per query word."""

    name = "fake"

    def __init__(
        self, pages: dict[str, str], results: list[SearchResult], *, blocked_search: bool = False
    ):
        self.pages, self.results = pages, results
        self.guard = Guard(max_pages=100, max_per_domain=20)
        self.blocked_search = blocked_search
        self.current: Page | None = None
        self.history: list[Page] = []
        self.opened: list[str] = []
        self.searches: list[str] = []

    async def start(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def search(self, query: str, limit: int) -> list[SearchResult]:
        self.searches.append(query)
        if self.blocked_search:
            raise SearchBlockedError("captcha")
        if query.startswith('"'):  # website lookup by company name
            name = query.strip('"').lower().split()[0]
            return [r for r in self.results if name in r.url][:limit]
        return list(self.results)[:limit]

    async def open_url(self, url: str) -> Page | None:
        if self.guard.refuse(url):
            return None
        self.guard.opened(url)
        self.opened.append(url)
        body = self.pages.get(url.rstrip("/")) or self.pages.get(url)
        if body is None:
            return None
        if body == "BLOCKED":
            self.guard.block(url, "bot check")
            return Page(url=url, title="Just a moment...", text="", blocked="bot check")
        page = extract.page_from_html(url, body)
        page.has_contact_form = "<textarea" in body
        self.current = page
        self.history.append(page)
        return page

    async def open(self, result: SearchResult) -> Page | None:
        return await self.open_url(result.url)

    async def follow(self, labels: list[str]) -> Page | None:
        """Like HttpBrowser: the current page's links, else an earlier page of the same site."""
        if self.current is None:
            return None
        for page in [self.current, *reversed(self.history)]:
            if link := extract.pick_link(page, labels, exclude=set(self.opened)):
                return await self.open_url(link.url)
        return None


def assessment(**kw) -> Assessment:
    base: dict[str, Any] = {
        "page_type": "company_product_page", "company_name": "", "company_website": "", "industry": "",
        "product": "", "builds_own_product": True, "sells_hardware_only": False,
        "project_signal": "product_development", "engineering_needs": [], "opportunity": "", "location": "",
        "evidence": [], "confidence": 0.8,
    }  # fmt: skip
    base.update(kw)
    base["evidence"] = [Quote(**q) if isinstance(q, dict) else q for q in base["evidence"]]
    return Assessment(**base)


class ScriptedModel:
    """lead.assess answers keyed by URL; counts calls so tests can prove the model was not asked."""

    def __init__(self, answers: dict[str, Assessment], fail: set[str] = frozenset()):  # type: ignore[assignment]
        self.answers, self.fail = answers, fail
        self.calls: list[str] = []

    async def __call__(self, url: str, title: str, text: str) -> Assessment:
        self.calls.append(url)
        if url in self.fail:
            from agentkit.llm import LLMError

            raise LLMError("model host down")
        return self.answers[url]


async def fake_draft(lead, contact):
    who = f"Hi {contact.name}," if contact.name else "Hello,"
    body = f"{who} we saw that {lead.company_name} builds {lead.product}. " + "We could help. " * 10
    return EmailDraft(subject=f"{lead.company_name}: platform engineering", body=body), []
