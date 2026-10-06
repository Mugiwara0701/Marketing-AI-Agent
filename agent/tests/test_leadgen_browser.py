"""Browser layer (no network): extraction, link choice, the HTTP backend's search/fetch handling, the desktop
adapter, the politeness guard; and the email writer's grounding checks."""

import asyncio

import httpx
import pytest

from agent import web
from agent.leadgen import extract, outreach
from agent.leadgen.browser import BudgetExhaustedError, Guard, SearchBlockedError
from agent.leadgen.browser.desktop import DesktopBrowser, engine_target
from agent.leadgen.browser.http import HttpBrowser, parse_ddg
from agent.leadgen.models import Contact, Evidence, Lead, Page, SearchResult

HOME = """<html><head><title>Acme EV</title></head><body>
<nav><a href="/contact">Contact us</a> <a href="https://acme-ev.com/team">Our Team</a>
<a href="https://evil.example/contact">Contact</a> <a href="mailto:sales@acme-ev.com">Mail</a>
<a href="javascript:void(0)">x</a></nav><script>var x="Contact";</script><p>We build chargers.</p></body></html>"""


def test_page_from_html_resolves_links_and_drops_scripts():
    page = extract.page_from_html("https://acme-ev.com/", HOME)
    assert (
        page.title == "Acme EV" and "var x" not in page.text and "We build chargers." in page.text
    )
    urls = [link.url for link in page.links]
    assert "https://acme-ev.com/contact" in urls and "mailto:sales@acme-ev.com" in urls
    assert not any(u.startswith("javascript") for u in urls)


def test_pick_link_only_follows_links_on_the_page_and_on_the_same_site():
    page = extract.page_from_html("https://acme-ev.com/", HOME)
    assert extract.pick_link(page, ["Contact us", "Contact"]).url == "https://acme-ev.com/contact"  # type: ignore[union-attr]
    assert extract.pick_link(page, ["Our team"]).url == "https://acme-ev.com/team"  # type: ignore[union-attr]
    assert extract.pick_link(page, ["Careers"]) is None  # never made up
    only_evil = Page("https://acme-ev.com/", "", "", [page.links[2]])
    assert extract.pick_link(only_evil, ["Contact"]) is None  # off-site link refused


def test_guard_budget_per_run_domain_and_blocked_hosts():
    g = Guard(max_pages=3, max_per_domain=2)
    for _ in range(2):
        assert g.refuse("https://a.com/x") is None
        g.opened("https://a.com/x")
    assert "already read 2 pages" in (g.refuse("https://www.a.com/y") or "")
    g.block("https://b.com/", "bot check")
    assert "blocked earlier" in (g.refuse("https://b.com/z") or "")
    g.opened("https://c.com/")
    with pytest.raises(BudgetExhaustedError):
        g.refuse("https://d.com/")


def test_ddg_html_parse():
    html = ('<a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Facme.io%2Fx">Acme '
            '<b>EV</b></a> <a class="result__snippet" href="#">Chargers on Linux</a>')  # fmt: skip
    (r,) = parse_ddg(html)
    assert (r.url, r.title, r.snippet, r.domain) == (
        "https://acme.io/x",
        "Acme EV",
        "Chargers on Linux",
        "acme.io",
    )


@pytest.fixture(autouse=True)
def _no_rendering(monkeypatch):
    """The JavaScript fallback (headless Chromium) must not run in unit tests: it would reach the network."""

    async def none(url):
        return None

    monkeypatch.setattr(web, "fetch_rendered", none)


class _Resp:
    def __init__(self, status, payload=None, text=""):
        self.status_code, self._payload, self.text = status, payload, text

    def json(self):
        return self._payload


def _fake_client(monkeypatch, resp):
    class C:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, *a, **k):
            if isinstance(resp, Exception):
                raise resp
            return resp

        post = get

    monkeypatch.setattr(httpx, "AsyncClient", C)


def test_http_search_reads_searxng(monkeypatch):
    monkeypatch.setenv("SEARXNG_URL", "http://127.0.0.1:8888")
    hits = {
        "results": [
            {"url": "https://acme.io/", "title": "Acme", "content": "EV", "engine": "bing"},
            {"x": 1},
        ]
    }
    _fake_client(monkeypatch, _Resp(200, hits))
    (r,) = asyncio.run(HttpBrowser(Guard()).search("q", 5))
    assert (r.url, r.engine, r.domain) == ("https://acme.io/", "searxng:bing", "acme.io")


def test_http_search_refusal_rests_the_engine(monkeypatch):
    monkeypatch.setenv("SEARXNG_URL", "http://127.0.0.1:8888")
    _fake_client(monkeypatch, _Resp(429, text="Too many requests"))
    b = HttpBrowser(Guard())
    with pytest.raises(SearchBlockedError):
        asyncio.run(b.search("q", 5))
    with pytest.raises(SearchBlockedError, match="resting"):
        asyncio.run(b.search("q", 5))


def test_http_search_unreachable_is_a_blocked_search(monkeypatch):
    monkeypatch.setenv("SEARXNG_URL", "http://127.0.0.1:8888")
    _fake_client(monkeypatch, httpx.ConnectError("down"))
    with pytest.raises(SearchBlockedError, match="unreachable"):
        asyncio.run(HttpBrowser(Guard()).search("q", 5))


def test_http_open_handles_redirects_blocks_and_failures(monkeypatch):
    answers = {
        "https://acme-ev.com": web.Fetched("https://www.acme-ev.com/en", 200, HOME),
        "https://cf.example": web.Fetched("https://cf.example", 403, "", "bot check"),
        "https://gone.example": web.Fetched("https://gone.example", 404, "nope"),
    }

    async def fake_fetch(url, **k):
        return answers.get(url)

    monkeypatch.setattr(web, "fetch_page", fake_fetch)
    b = HttpBrowser(Guard())
    page = asyncio.run(b.open_url("https://acme-ev.com"))
    assert page and page.url == "https://www.acme-ev.com/en"  # the final address after redirects
    blocked = asyncio.run(b.open_url("https://cf.example"))
    assert blocked and blocked.blocked == "bot check" and "cf.example" in b.guard.blocked_hosts
    assert asyncio.run(b.open_url("https://cf.example/other")) is None  # not retried this run
    assert asyncio.run(b.open_url("https://gone.example")) is None
    assert (
        asyncio.run(b.open_url("https://timeout.example")) is None
    )  # fetch failed: skipped, no crash


def test_http_follow_uses_earlier_pages_of_the_site(monkeypatch):
    pages = {
        "https://acme-ev.com": web.Fetched("https://acme-ev.com/", 200, HOME),
        "https://acme-ev.com/contact": web.Fetched(
            "https://acme-ev.com/contact", 200, "<p>sales@acme-ev.com</p>"
        ),
        "https://acme-ev.com/team": web.Fetched("https://acme-ev.com/team", 200, "<p>Ana, CTO</p>"),
    }

    async def fake_fetch(url, **k):
        return pages.get(url.rstrip("/"))

    monkeypatch.setattr(web, "fetch_page", fake_fetch)
    b = HttpBrowser(Guard())
    asyncio.run(b.open_url("https://acme-ev.com"))
    assert asyncio.run(b.follow(["Contact us"])).url.endswith("/contact")  # type: ignore[union-attr]
    assert asyncio.run(b.follow(["Our team"])).url.endswith("/team")  # type: ignore[union-attr]
    assert asyncio.run(b.follow(["Contact us"])) is None  # already read: not again


class FakeDesk:
    def __init__(self, screens):
        self.screens, self.url, self.typed, self.last_look = screens, "", [], None

    async def navigate(self, target):
        self.typed.append(target)
        self.url = (
            target if target.startswith("http") else f"https://www.google.com/search?q={target}"
        )

    async def read_page(self):
        return self.screens.get(self.url, "Some page text " * 20)

    async def dismiss_consent(self):
        return False

    async def shot(self, label):
        return ""

    async def title(self):
        return "title"

    async def current_url(self):
        return self.url

    async def open_link(self, title, results_url):
        self.url = "https://acme-ev.com/"
        return self.url

    async def recover(self):
        return None


def test_desktop_adapter_rests_a_blocked_engine_and_reads_results_with_the_model(monkeypatch):
    monkeypatch.delenv("SEARXNG_URL", raising=False)  # the path without SearXNG
    from agent.tasks import search as serp

    captcha = (
        "Our systems have detected unusual traffic from your computer network. I'm not a robot"
    )
    desk = FakeDesk({"https://www.google.com/search?q=ev charger aosp": captcha})

    async def read(text, query):
        return serp.SearchRead(
            hits=[serp.Hit(title="Acme EV chargers", domain="acme-ev.com", snippet="Linux")]
        )

    monkeypatch.setattr(serp, "read_results", read)
    b = DesktopBrowser(Guard(), ["default", "https://duckduckgo.com/?q={q}"], 30, desk=desk)  # type: ignore[arg-type]
    (r,) = asyncio.run(b.search("ev charger aosp", 5))
    assert (
        desk.typed[0] == "ev charger aosp" and "duckduckgo" in desk.typed[1]
    )  # google rested, next engine used
    assert not b.guard.engine_ready("default") and r.domain == "acme-ev.com" and r.url == ""
    page = asyncio.run(b.open(r))
    assert page and page.url == "https://acme-ev.com/"
    assert (
        engine_target("https://www.bing.com/search?q={q}", "a b")
        == "https://www.bing.com/search?q=a+b"
    )


def test_email_grounding_flags_invented_technology():
    lead = Lead(company_name="Acme EV", company_website="acme-ev.com", product="chargers",
                technical_requirements=["embedded_linux"], project_signal="product_development",
                evidence=[Evidence(url="u", reason="runs embedded Linux", quote="runs embedded Linux")])  # fmt: skip
    ctx = outreach.lead_context(lead, Contact(name="Ana", role="CTO", email="a@acme-ev.com"))
    assert (
        "Ana" in ctx and "They have NOT asked for anything" in ctx and "runs embedded Linux" in ctx
    )
    assert outreach.ungrounded_tech("We can port Yocto and your embedded Linux.", ctx)[
        0
    ].startswith("mentions 'Yocto'")
    assert outreach.ungrounded_tech("We can help with your embedded Linux.", ctx) == []


def test_email_writer_flags_generic_drafts(monkeypatch):
    from agent.tasks import proposal

    async def generic(ctx):
        return proposal.EmailDraft(
            subject="Engineering services", body="We offer Android services. " * 6
        ), []

    monkeypatch.setattr(proposal, "draft_proposal", generic)
    lead = Lead(company_name="Acme EV", company_website="acme-ev.com")
    _, problems = asyncio.run(outreach.draft(lead, Contact(email="a@acme-ev.com")))
    assert any("does not name the company" in p for p in problems)


def test_search_result_dataclass_defaults():
    r = SearchResult("t")
    assert r.url == "" and r.snippet == ""


def test_a_results_page_the_model_cannot_read_skips_the_query(monkeypatch):
    monkeypatch.delenv("SEARXNG_URL", raising=False)  # the path without SearXNG
    from agent.tasks import search as serp
    from agentkit.llm import LLMError

    async def slow(text, query):
        raise LLMError("llm too slow")

    monkeypatch.setattr(serp, "read_results", slow)
    b = DesktopBrowser(Guard(), ["default"], 30, desk=FakeDesk({}))  # type: ignore[arg-type]
    with pytest.raises(SearchBlockedError, match="could not read the results page"):
        asyncio.run(b.search("ev charger aosp", 5))


def test_no_query_suffix_is_typed_by_default():
    from agent.leadgen import config

    assert not config.load().search.get("query_suffix")


def test_slow_model_is_not_asked_again_and_cpu_limits_are_raised(monkeypatch):
    from agent.leadgen import service
    from agentkit import llm

    calls = []

    class C:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            calls.append(1)
            raise httpx.ReadTimeout("slow")

    monkeypatch.setenv("LLM_BASE_URL", "http://127.0.0.1:1")
    monkeypatch.setattr(httpx, "AsyncClient", C)
    with pytest.raises(llm.LLMError, match="too slow"):
        asyncio.run(llm._post({}, 5))
    assert calls == [1]  # not re-queued behind itself
    monkeypatch.delenv("LLM_MIN_TIMEOUT", raising=False)
    monkeypatch.setattr(service.shutil, "which", lambda name: None)
    service.cpu_model_time_limits()
    import os

    assert os.environ["LLM_MIN_TIMEOUT"] == "420"


class _Lookup:
    name = "lookup"

    def __init__(self):
        self.guard = Guard()

    async def search(self, query, limit):
        return [
            SearchResult(
                "Acme EV chargers",
                "https://acme-ev.com/",
                "Linux chargers",
                "searxng:bing",
                "acme-ev.com",
            )
        ]


def test_desktop_with_searxng_needs_no_model_and_opens_by_address(monkeypatch):
    from agent.tasks import search as serp

    async def must_not_run(text, query):
        raise AssertionError("the model must not read results when SearXNG gives the links")

    monkeypatch.setattr(serp, "read_results", must_not_run)
    monkeypatch.setenv("SEARXNG_URL", "http://127.0.0.1:8888")
    desk = FakeDesk({})
    b = DesktopBrowser(Guard(), ["default"], 30, desk=desk, lookup=_Lookup())  # type: ignore[arg-type]
    (r,) = asyncio.run(b.search("kiosk company building Yocto", 5))
    assert desk.typed == [
        "http://127.0.0.1:8888/search?q=kiosk+company+building+Yocto"
    ]  # shown in the window
    page = asyncio.run(b.open(r))
    assert page and page.url == "https://acme-ev.com/" and desk.typed[-1] == "https://acme-ev.com/"


def test_desktop_never_takes_a_search_page_for_a_result(monkeypatch):
    monkeypatch.setenv("SEARXNG_URL", "http://127.0.0.1:8888")
    stale = "https://duckduckgo.com/?q=smart+card+reader+manufacturer+-internship"
    b = DesktopBrowser(Guard(), ["default"], 30, desk=FakeDesk({}), lookup=_Lookup())  # type: ignore[arg-type]
    assert asyncio.run(b.open_url(stale)) is None
    assert asyncio.run(b.open_url("http://127.0.0.1:8888/search?q=x")) is None
