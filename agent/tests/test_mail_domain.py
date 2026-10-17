"""contacts.mail_domain_ok: addresses that can only bounce are dropped before a draft is written."""

import asyncio

import dns.asyncresolver
import dns.exception
import dns.resolver
import pytest

from agent.leadgen import contacts


@pytest.fixture(autouse=True)
def _fresh_cache():
    contacts._MAIL_DOMAINS.clear()
    yield
    contacts._MAIL_DOMAINS.clear()


def _dns(monkeypatch, answers: dict[tuple[str, str], object]) -> list[tuple[str, str]]:
    asked: list[tuple[str, str]] = []

    async def resolve(domain, kind, **_kw):
        asked.append((domain, kind))
        what = answers.get((domain, kind), dns.resolver.NoAnswer())
        if isinstance(what, Exception):
            raise what
        return what

    monkeypatch.setattr(dns.asyncresolver, "resolve", resolve)
    return asked


def test_a_domain_with_mx_is_ok(monkeypatch):
    _dns(monkeypatch, {("acme.io", "MX"): object()})
    assert asyncio.run(contacts.mail_domain_ok("sales@Acme.io")) is True


def test_a_records_count_when_there_is_no_mx(monkeypatch):
    _dns(monkeypatch, {("acme.io", "A"): object()})
    assert asyncio.run(contacts.mail_domain_ok("sales@acme.io")) is True


def test_a_missing_domain_is_not_ok_and_is_cached(monkeypatch):
    asked = _dns(monkeypatch, {("gone.example", "MX"): dns.resolver.NXDOMAIN()})
    assert asyncio.run(contacts.mail_domain_ok("a@gone.example")) is False
    assert asyncio.run(contacts.mail_domain_ok("b@gone.example")) is False
    assert asked == [("gone.example", "MX")]


def test_no_records_at_all_is_not_ok(monkeypatch):
    _dns(monkeypatch, {})
    assert asyncio.run(contacts.mail_domain_ok("a@empty.example")) is False


def test_a_dns_timeout_is_unknown_not_bad(monkeypatch):
    _dns(monkeypatch, {("slow.example", "MX"): dns.exception.Timeout()})
    assert asyncio.run(contacts.mail_domain_ok("a@slow.example")) is None


def test_not_an_address(monkeypatch):
    _dns(monkeypatch, {})
    assert asyncio.run(contacts.mail_domain_ok("no-at-sign")) is False
