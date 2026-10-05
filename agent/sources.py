"""Free public sources: job/project listings for leads, news and discussions for blog research.

Every function is best-effort: a source that is down, changed or blocks us returns [] and the run
continues with the others.
"""

import hashlib
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html import unescape
from urllib.parse import parse_qs, urlparse

import httpx
import yaml

from agentkit.config import env
from agentkit.log import get_logger

from . import settings, web

log = get_logger("agent.sources")


@dataclass
class Signal:
    """One public listing that might show a company needs AOSP / embedded engineering work."""

    kind: str  # job_post | project_post | web_page | company_page
    source: str  # hn_hiring | remoteok | remotive | weworkremotely | feed | search | company_search
    url: str
    title: str
    text: str
    company_hint: str = ""
    domain_hint: str = ""  # company website when the source knows it (company hiring feeds)
    published: float | None = None  # epoch seconds when the source says when it was posted
    hash: str = field(init=False)

    def __post_init__(self) -> None:
        self.hash = hashlib.sha256(f"{self.url}|{self.text[:2000]}".encode()).hexdigest()


@dataclass
class Item:
    """One article/discussion for blog research."""

    source: str
    title: str
    url: str
    summary: str = ""
    score: int = 0  # points / reactions where the source has them
    comments: int = 0
    published: float | None = None  # epoch seconds when known
    heat: float = 0.0  # popularity x freshness, set by research.rank()


def load_config() -> dict:
    with open(settings.load().sources_file, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def matches(text: str, keywords: list[str]) -> bool:
    """Whole-word / whole-phrase match, case-insensitive ('bsp' must not match 'websphere')."""
    low = (text or "").lower()
    return any(
        re.search(rf"(?<![a-z0-9]){re.escape(k.lower())}(?![a-z0-9])", low) for k in keywords
    )


def strip_html(s: str) -> str:
    return re.sub(r"\s+", " ", web.html_to_text(unescape(s or ""))).strip()


async def _json(url: str, **params) -> object | None:
    headers = {"User-Agent": settings.load().user_agent}
    try:
        async with httpx.AsyncClient(headers=headers, timeout=25, follow_redirects=True) as c:
            r = await c.get(url, params=params)
        r.raise_for_status()
        return r.json()
    except (httpx.HTTPError, ValueError):
        log.warning("source unavailable", extra={"ctx": {"host": urlparse(url).hostname}})
        return None


def _parse_feed(xml_text: str) -> list[dict]:
    """RSS 2.0 and Atom -> [{title, link, summary}]."""
    try:
        root = ET.fromstring(xml_text)  # noqa: S314
    except ET.ParseError:
        return []
    out = []
    for el in root.iter():
        tag = el.tag.rsplit("}", 1)[-1]
        if tag not in ("item", "entry"):
            continue
        d = {"title": "", "link": "", "summary": "", "date": ""}
        for ch in el:
            t = ch.tag.rsplit("}", 1)[-1]
            if t == "title":
                d["title"] = (ch.text or "").strip()
            elif t == "link":
                d["link"] = (ch.get("href") or ch.text or "").strip() or d["link"]
            elif t in ("pubDate", "published", "updated", "date") and not d["date"]:
                d["date"] = (ch.text or "").strip()
            elif t in ("description", "summary", "content") and not d["summary"]:
                d["summary"] = strip_html(ch.text or "")[:1200]
        if d["title"] and d["link"]:
            out.append(d)
    return out


async def _feed(url: str) -> list[dict]:
    body = await web.fetch(
        url, check_robots=False, accept="application/rss+xml, application/atom+xml, text/xml"
    )
    return _parse_feed(body) if body else []


# ----------------------------------------------------------------------------- lead sources


async def _hn_threads() -> list[tuple[str, str]]:
    """(story id, kind) of the latest 'Who is hiring' and 'Seeking freelancer' threads."""
    data = await _json(
        "https://hn.algolia.com/api/v1/search_by_date",
        tags="story,author_whoishiring", hitsPerPage=8,
    )  # fmt: skip
    out: list[tuple[str, str]] = []
    for h in (data or {}).get("hits", []) if isinstance(data, dict) else []:
        t = (h.get("title") or "").lower()
        if "who is hiring" in t and not any(k == "job_post" for _, k in out):
            out.append((h["objectID"], "job_post"))
        elif "freelancer" in t and "seeking" in t and not any(k == "project_post" for _, k in out):
            out.append((h["objectID"], "project_post"))
    return out


async def _hn_hiring(keywords: list[str]) -> list[Signal]:
    seen: dict[str, Signal] = {}
    for story_id, kind in await _hn_threads():
        for kw in keywords[:10]:
            data = await _json(
                "https://hn.algolia.com/api/v1/search",
                query=f'"{kw}"', tags=f"comment,story_{story_id}", hitsPerPage=40,
            )  # fmt: skip
            for h in (data or {}).get("hits", []) if isinstance(data, dict) else []:
                text = strip_html(h.get("comment_text") or "")
                url = f"https://news.ycombinator.com/item?id={h.get('objectID')}"
                if text and matches(text, keywords):
                    seen[url] = Signal(kind, "hn_hiring", url, text[:100], text[:3000],
                                       published=h.get("created_at_i"))  # fmt: skip
    return list(seen.values())


async def _remoteok(keywords: list[str]) -> list[Signal]:
    data = await _json("https://remoteok.com/api")
    out = []
    for j in data if isinstance(data, list) else []:
        if not isinstance(j, dict) or not j.get("position"):
            continue
        text = strip_html(
            f"{j.get('position')} {' '.join(j.get('tags') or [])} {j.get('description') or ''}"
        )
        if matches(text, keywords):
            out.append(Signal("job_post", "remoteok", j.get("url") or "", j["position"],
                              f"Company: {j.get('company', '')}. {text[:3000]}", j.get("company", ""),
                              published=j.get("epoch")))  # fmt: skip
    return out


async def _remotive(keywords: list[str]) -> list[Signal]:
    from . import portals  # noqa: PLC0415

    if not portals.due("remotive"):  # Remotive allows at most 4 calls per day
        return []
    out: dict[str, Signal] = {}
    for kw in ("AOSP", "embedded linux"):
        data = await _json("https://remotive.com/api/remote-jobs", search=kw, limit=50)
        for j in (data or {}).get("jobs", []) if isinstance(data, dict) else []:
            text = strip_html(f"{j.get('title')} {j.get('description') or ''}")
            if matches(text, keywords):
                out[j["url"]] = Signal("job_post", "remotive", j["url"], j.get("title", ""),
                                       f"Company: {j.get('company_name', '')}. {text[:3000]}",
                                       j.get("company_name", ""),
                                       published=_iso_epoch(j.get("publication_date")))  # fmt: skip
    return list(out.values())


async def _arbeitnow(keywords: list[str]) -> list[Signal]:
    data = await _json("https://www.arbeitnow.com/api/job-board-api")
    out = []
    for j in (data or {}).get("data", []) if isinstance(data, dict) else []:
        text = strip_html(
            f"{j.get('title')} {' '.join(j.get('tags') or [])} {j.get('description') or ''}"
        )
        if matches(text, keywords):
            out.append(Signal("job_post", "arbeitnow", j.get("url", ""), j.get("title", ""),
                              f"Company: {j.get('company_name', '')}. Location: {j.get('location', '')}. {text[:3000]}",
                              j.get("company_name", ""), published=j.get("created_at")))  # fmt: skip
    return out


async def _jobicy(keywords: list[str]) -> list[Signal]:
    from . import portals  # noqa: PLC0415

    if not portals.due("jobicy"):  # poll at most hourly
        return []
    out: dict[str, Signal] = {}
    for tag in ("android", "embedded", "linux"):
        data = await _json("https://jobicy.com/api/v2/remote-jobs", count=50, tag=tag)
        for j in (data or {}).get("jobs", []) if isinstance(data, dict) else []:
            text = strip_html(f"{j.get('jobTitle')} {j.get('jobDescription') or ''}")
            if matches(text, keywords):
                out[j["url"]] = Signal("job_post", "jobicy", j["url"], j.get("jobTitle", ""),
                                       f"Company: {j.get('companyName', '')}. {text[:3000]}",
                                       j.get("companyName", ""))  # fmt: skip
    return list(out.values())


async def _feed_signals(url: str, source: str, keywords: list[str]) -> list[Signal]:
    out = []
    for e in await _feed(url):
        text = f"{e['title']}. {e['summary']}"
        if matches(text, keywords):
            out.append(Signal("job_post", source, e["link"], e["title"], text[:3000]))
    return out


def _ddg_results(html: str) -> list[tuple[str, str]]:
    """(url, title) pairs from DuckDuckGo's HTML endpoint."""
    out = []
    for m in re.finditer(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', html, re.S):
        href = unescape(m.group(1))
        if "uddg=" in href:
            href = parse_qs(urlparse(href).query).get("uddg", [""])[0]
        if href.startswith("//"):
            href = "https:" + href
        out.append((href, strip_html(m.group(2))))
    return out


async def web_search(query: str, limit: int = 8) -> list[tuple[str, str]]:
    """(url, title) results. Prefers a self-hosted SearXNG (SEARXNG_URL, free, JSON enabled);
    falls back to DuckDuckGo's HTML page, which usually answers bots with a challenge page."""
    searx = env("SEARXNG_URL")
    try:
        if searx:
            data = await _json(f"{searx.rstrip('/')}/search", q=query, format="json")
            hits = (data or {}).get("results", []) if isinstance(data, dict) else []
            return [(h["url"], h.get("title", "")) for h in hits if h.get("url")][:limit]
        async with httpx.AsyncClient(
            headers={"User-Agent": "Mozilla/5.0 (compatible; research bot)"}, timeout=25
        ) as c:
            r = await c.post("https://html.duckduckgo.com/html/", data={"q": query})
        results = _ddg_results(r.text) if r.status_code == 200 else []
    except httpx.HTTPError:
        results = []
    if not results:
        log.warning("web search returned nothing (set SEARXNG_URL for reliable free search)")
    return results[:limit]


_MONTHS = "jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec"
_DATE_YEARS = re.compile(
    rf"(?:\b(?:{_MONTHS})[a-z]*\.?\s+(?:\d{{1,2}}(?:st|nd|rd|th)?,?\s+)?(20\d\d)\b"  # March 2025, Mar 3, 2025
    r"|\b(20\d\d)-\d\d-\d\d\b"  # 2025-03-14
    r"|\b\d{1,2}[./]\d{1,2}[./](20\d\d)\b"  # 14.03.2025
    r"|/(20\d\d)/\d\d/)",  # /2025/03/ in a URL
    re.I,
)


def _iso_epoch(value) -> float | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def is_stale(sig: Signal, max_age_days: int, now: float | None = None) -> bool:
    """True if the post is older than max_age_days. Uses the source's date when it has one; otherwise the newest
    date written in the text or URL (company pages are never judged by text, products do not expire)."""
    now = now or time.time()
    if sig.published:
        return now - float(sig.published) > max_age_days * 86400
    if sig.kind == "company_page":
        return False
    years = [
        int(y)
        for m in _DATE_YEARS.finditer(f"{sig.title} {sig.url} {sig.text}")
        for y in m.groups()
        if y
    ]
    if not years:
        return False  # no date to judge by: keep it, the LLM and the human review decide
    newest_possible = datetime(max(years), 12, 31, tzinfo=UTC).timestamp()  # most generous reading
    return now - newest_possible > max_age_days * 86400


def is_project_work(sig: Signal, project_kw: list[str]) -> bool:
    """Contract / freelance / outsourced project work, not a permanent job. Company pages (their own products)
    and freelancer-thread posts always pass; anything else needs a project word such as 'contract' or 'RFP'."""
    if sig.kind in ("company_page", "project_post"):
        return True
    return matches(f"{sig.title} {sig.text}", project_kw)


def balance(signals: list[Signal], max_items: int, per_company: int = 2) -> list[Signal]:
    """At most `per_company` postings per company, then one listing from each source in turn, so a
    single large board cannot crowd out the others when the list is cut to `max_items`."""
    count: dict[str, int] = {}
    kept: list[Signal] = []
    for s in signals:
        key = (s.domain_hint or s.company_hint or s.url).lower()
        count[key] = count.get(key, 0) + 1
        if count[key] <= per_company:
            kept.append(s)
    by_source: dict[str, list[Signal]] = {}
    for s in kept:
        by_source.setdefault(s.source, []).append(s)
    out: list[Signal] = []
    while any(by_source.values()) and len(out) < max_items:
        for items in by_source.values():
            if items and len(out) < max_items:
                out.append(items.pop(0))
    return out


async def collect_signals(max_items: int) -> list[Signal]:
    """Listings from the free job / project feeds (APIs and RSS), de-duplicated, keyword-filtered, capped.
    Web search is not done here: the lead pipeline's research agent searches with its browser."""
    cfg = load_config()
    kws: list[str] = cfg.get("keywords", [])
    apis: dict = cfg.get("job_apis", {})
    found: list[Signal] = []
    if apis.get("hn_hiring"):
        found += await _hn_hiring(kws)
    if apis.get("remoteok"):
        found += await _remoteok(kws)
    if apis.get("remotive"):
        found += await _remotive(kws)
    if apis.get("arbeitnow"):
        found += await _arbeitnow(kws)
    if apis.get("jobicy"):
        found += await _jobicy(kws)
    if apis.get("weworkremotely"):
        found += await _feed_signals(
            "https://weworkremotely.com/categories/remote-programming-jobs.rss",
            "weworkremotely",
            kws,
        )
    for url in cfg.get("extra_lead_feeds", []):
        found += await _feed_signals(url, "feed", kws)
    from . import portals  # noqa: PLC0415 - portals imports Signal from this module

    found += await portals.company_boards(cfg.get("company_boards") or {}, kws)
    for fn, flag in ((portals.himalayas, "himalayas"), (portals.themuse, "themuse"),
                     (portals.adzuna, "adzuna"), (portals.jooble, "jooble")):  # fmt: skip
        if apis.get(flag, True):
            found += await fn(kws)
    max_age = int(cfg.get("max_age_days", 45))
    fresh = [s for s in found if not is_stale(s, max_age)]
    log.info(
        "dropped old posts",
        extra={"ctx": {"old": len(found) - len(fresh), "max_age_days": max_age}},
    )
    found = fresh
    if cfg.get("work_type", "all") == "projects":
        pk = cfg.get("project_keywords") or []
        before = len(found)
        found = [s for s in found if is_project_work(s, pk)]
        log.info("kept project work only", extra={"ctx": {"before": before, "after": len(found)}})
    picked = balance(list({s.hash: s for s in found}.values()), max_items)
    log.info("signals collected", extra={"ctx": {"count": len(picked)}})
    return picked


# ----------------------------------------------------------------------------- blog research


__all__ = ["Item", "Signal", "collect_signals", "web_search"]
