"""The crawling rules every browser backend applies (web.may_visit) and the redirect checks of fetch_page."""

import asyncio

import httpx

from agent import web


def _client_factory(handler):
    real = httpx.AsyncClient

    def make(*args, **kwargs):
        kwargs.pop("transport", None)
        return real(*args, transport=httpx.MockTransport(handler), **kwargs)

    return make


def test_may_visit_refuses_portals_private_addresses_and_odd_schemes(monkeypatch):
    monkeypatch.setattr(web, "_public_host", lambda host: host not in ("127.0.0.1", "10.0.0.5"))
    assert "portal" in (asyncio.run(web.may_visit("https://in.indeed.com/jobs")) or "")
    assert "public" in (asyncio.run(web.may_visit("http://10.0.0.5/admin")) or "")
    assert asyncio.run(web.may_visit("file:///etc/passwd")) == "not a web address"


def test_may_visit_obeys_robots_txt(monkeypatch):
    web._robots.clear()
    monkeypatch.setattr(web, "_public_host", lambda host: True)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/robots.txt"
        return httpx.Response(200, text="User-agent: *\nDisallow: /private\n")

    monkeypatch.setattr(web.httpx, "AsyncClient", _client_factory(handler))
    assert asyncio.run(web.may_visit("https://site.example/about")) is None
    assert "robots" in (asyncio.run(web.may_visit("https://site.example/private/x")) or "")


def test_a_redirect_to_a_private_address_is_not_followed(monkeypatch):
    web._robots.clear()
    monkeypatch.setattr(web, "_HOST_DELAY", 0)
    monkeypatch.setattr(web, "_public_host", lambda host: host != "10.0.0.5")
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(302, headers={"location": "http://10.0.0.5/secret"})

    monkeypatch.setattr(web.httpx, "AsyncClient", _client_factory(handler))
    got = asyncio.run(web.fetch_page("https://public.example/page"))
    assert got is not None and got.blocked == "redirected to a non-public address"
    assert not any("10.0.0.5" in u for u in seen)  # never requested


def test_a_huge_body_is_cut(monkeypatch):
    web._robots.clear()
    monkeypatch.setattr(web, "_HOST_DELAY", 0)
    monkeypatch.setattr(web, "_public_host", lambda host: True)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, text="x" * (web._MAX_BYTES * 2))

    monkeypatch.setattr(web.httpx, "AsyncClient", _client_factory(handler))
    got = asyncio.run(web.fetch_page("https://big.example/"))
    assert got is not None and got.status == 200 and len(got.html) == web._MAX_BYTES
