"""Desktop lead discovery: a visible Chrome window on the Xubuntu desktop is used like a person would use it.

    type a search -> read the results page -> click a promising result -> read the page -> judge it ->
    look up the company's own site and contact page the same way -> store the lead and an email DRAFT

Everything the agent learns comes from what Chrome shows (selected text copied from the page, OCR, screenshots).
It never calls a site's hidden API, never reads the DOM, and never tries to get past a CAPTCHA or bot check:
a blocked site or engine is logged, rested, and the run moves to another source. Emails are never sent.
"""

import asyncio
import hashlib
import json
import os
import re
import shutil
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import quote_plus, urlparse

from agentkit.config import env
from agentkit.log import get_logger

from .. import contacts, mailer, notify, settings, sources, store, web
from .. import leads as lead_pipeline
from ..tasks import proposal, qualify, search
from . import leadscore
from .desktop import Desktop, DesktopError, preflight
from .executor_client import ExecutorError
from .policy import Policy

log = get_logger("agent.discover")

# Link texts a person clicks to find a company's contact details. No URL is ever guessed: a link that is not on
# the page is not visited.
_CONTACT_LABELS = (  # tried in this order; each group is "any of these link texts"
    ("Contact us", "Contact", "Get in touch"),
    ("About us", "About", "Imprint", "Impressum"),
)
_MAX_CONTACT_PAGES = 4
_DOMAIN = re.compile(
    r"(?<![@\w.-])((?:[a-z0-9-]+\.)+(?:com|io|ai|net|org|co|de|eu|in|tech|dev|app|cloud|systems|"
    r"se|fr|uk|us|nl|jp|kr|cn|tw|it|es|ch|at|pl|ca|au|fi|no|dk|il|sg))\b",
    re.I,
)
_UNREACHABLE = re.compile(
    r"this site can.t be reached|err_[a-z_]+|dns_probe|server ip address could not", re.I
)
_NOT_FOUND = re.compile(
    r"404|page (could not|couldn.t|can.t|cannot) be found|page not found|not found|no longer available",
    re.I,
)
_MAX_VISITED = 800


# --- pure helpers (unit tested) --------------------------------------------------------------------


def engine_target(engine: str, query: str) -> str:
    """What is typed into the address bar: the bare query for Chrome's own search, else the engine URL."""
    return query if engine == "default" else engine.format(q=quote_plus(query))


def build_queue(cfg: dict) -> deque[tuple[str, str]]:
    """(platform, query) pairs. The open web first for every query, then each platform in turn."""
    platforms = cfg.get("platforms") or [{"name": "web", "template": "{q}"}]
    base = [q for q in cfg.get("queries") or [] if q]
    web_p = next((p for p in platforms if p["name"] == "web"), platforms[0])
    others = [p for p in platforms if p is not web_p]
    order: list[tuple[str, str]] = [(web_p["name"], web_p["template"].format(q=q)) for q in base]
    for i, q in enumerate(base):
        for j, p in enumerate(others):
            if (
                i + j
            ) % 2 == 0:  # about half of the platform combinations: variety without hammering
                order.append((p["name"], p["template"].format(q=q)))
    for i, q in enumerate(base):
        for j, p in enumerate(others):
            if (i + j) % 2 == 1:
                order.append((p["name"], p["template"].format(q=q)))
    return deque(order)


def domain_from_page(url: str, company: str) -> str:
    """The page's own domain when it is clearly the company's website (the name appears in the host)."""
    dom = web.registrable_domain(url)
    label = dom.split(".")[0] if dom else ""
    tokens = [t for t in re.split(r"\W+", company.lower()) if len(t) >= 3][:3]
    return (
        dom
        if label and web.is_company_site(dom) and any(t in label or label in t for t in tokens)
        else ""
    )


def domains_in(text: str, company: str) -> str:
    """First domain in a results page that looks like the company's own site (name token in its label)."""
    tokens = [t for t in re.split(r"\W+", company.lower()) if len(t) >= 3][:2]
    for m in _DOMAIN.finditer(text):
        dom = web.registrable_domain(m.group(1))
        if (
            dom
            and web.is_company_site(dom)
            and not web.blocked(dom)
            and any(t in dom.split(".")[0] for t in tokens)
        ):
            return dom
    return ""


def lead_record(*, q, hit_title: str, url: str, domain: str, text: str, score: int, parts: dict,
                contact, keywords: list[str], query: str, engine: str) -> dict:  # fmt: skip
    """Everything we know about a lead, stored with the signal so a person can check it."""
    fit = set(q.service_fit)
    return {
        "company": q.company_name,
        "title": hit_title,
        "description": q.project_summary,
        "technologies": q.technologies,
        "aosp_relevance": "AOSP" in fit,
        "android_relevance": bool(fit & {"AOSP", "Android HAL", "Automotive"}),
        "embedded_relevance": bool(fit & {"BSP", "Embedded Linux"}),
        "why_it_matches": q.reason,
        "opportunity_type": leadscore.opportunity_type(text, url),
        "source": engine,
        "search_query": query,
        "source_url": url,
        "company_website": f"https://{domain}",
        "contact": {"email": contact.email, "name": contact.name or None, "role": contact.role or None} if contact else None,
        "location": q.location or None,
        "qualification_score": score,
        "score_parts": parts,
        "evidence": leadscore.evidence_snippet(text, keywords),
        "discovered_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "status": "contact_found" if contact else "qualified",
    }  # fmt: skip


# --- run state -------------------------------------------------------------------------------------


@dataclass
class State:
    """Saved after every search, so a crash or restart on the same day continues instead of starting over."""

    day: str
    done: list[str] = field(default_factory=list)
    visited: list[str] = field(default_factory=list)
    rest_until: dict[str, float] = field(default_factory=dict)  # engine -> epoch seconds
    path: Path = field(default_factory=lambda: Path("out/desktop/state.json"))

    @classmethod
    def load(cls, path: Path) -> "State":
        today = datetime.now(UTC).date().isoformat()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("day") == today:
                return cls(path=path, **{k: v for k, v in data.items() if k != "path"})
        except (OSError, ValueError, TypeError):
            pass
        return cls(day=today, path=path)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {**asdict(self), "path": str(self.path), "visited": self.visited[-_MAX_VISITED:]}
        self.path.write_text(json.dumps(data), encoding="utf-8")


@dataclass
class Ctx:
    desk: Desktop
    cfg: dict  # the `desktop:` block of sources.yaml
    keywords: list[str]
    max_age_days: int
    state: State
    deadline: float
    stats: dict
    min_score: int
    hits_per_query: int
    min_hit_score: float
    min_keywords: int = 2
    min_confidence: float = 0.7
    blocked: set[str] = field(default_factory=set)
    domains_seen: set[str] = field(default_factory=set)
    engine_name: str = "default"


# --- the browser steps -----------------------------------------------------------------------------


def _engines(ctx: Ctx) -> list[str]:
    return ctx.cfg.get("engines") or ["default"]


def _pick_engine(ctx: Ctx, n: int) -> str | None:
    """Round-robin over engines that are not resting."""
    engines = _engines(ctx)
    now = time.time()
    for i in range(len(engines)):
        e = engines[(n + i) % len(engines)]
        if ctx.state.rest_until.get(e, 0) <= now:
            return e
    return None


def _rest(ctx: Ctx, key: str, why: str) -> None:
    minutes = float(ctx.cfg.get("engine_rest_minutes", 30))
    ctx.state.rest_until[key] = time.time() + minutes * 60
    if key not in ctx.blocked:
        ctx.blocked.add(key)
        ctx.stats["blocked_sources"] += 1
    log.warning(
        "Website blocked", extra={"ctx": {"source": key, "why": why, "rest_minutes": minutes}}
    )


async def _read_checked(ctx: Ctx, label: str) -> tuple[str, str | None]:
    """Read the page on screen. Handles a cookie banner. Returns (text, block reason or None)."""
    desk = ctx.desk
    text = await desk.read_page()
    if leadscore.is_consent_page(text) and await desk.dismiss_consent():
        text = await desk.read_page()
    await desk.shot(label)
    why = leadscore.block_reason(text, await desk.title())
    if not why and desk.last_look and desk.last_look.blocked:
        why = "the vision model sees a CAPTCHA, login wall or access-denied page"
    if desk.last_look and desk.last_look.summary:
        log.info(
            "Screen seen", extra={"ctx": {"label": label[:60], "summary": desk.last_look.summary}}
        )
    return text, why


async def search_page(ctx: Ctx, query: str, n: int) -> tuple[str, str, str] | None:
    """Type `query` into a search engine and read the results. (text, results_url, engine) or None."""
    desk = ctx.desk
    for _ in range(len(_engines(ctx))):
        engine = _pick_engine(ctx, n)
        if engine is None:
            return None
        log.info("Search started", extra={"ctx": {"engine": engine, "query": query}})
        suffix = str(ctx.cfg.get("query_suffix") or "").strip()
        await desk.navigate(engine_target(engine, f"{query} {suffix}".strip()))
        text, why = await _read_checked(ctx, f"search {query}")
        if why:
            _rest(ctx, engine, why)
            n += 1
            continue
        await desk.scroll(2)
        return text, await desk.current_url(), engine
    return None


async def find_website(ctx: Ctx, company: str) -> str:
    """The company's own website, found by searching for it in the window."""
    got = await search_page(ctx, f"{company} official website", ctx.stats["searches"])
    return domains_in(got[0], company) if got else ""


def _page_missing(desk: Desktop, title: str, text: str) -> bool:
    """A 404 / error page, by what the vision model saw or by the title and text."""
    seen = desk.last_look
    return bool(seen and seen.not_found) or bool(
        _NOT_FOUND.search(title) or _UNREACHABLE.search(text[:500])
    )


async def visible_contact(ctx: Ctx, domain: str):
    """Open the company's home page in the window, then click its Contact / About link (header, else footer)
    and choose a public business email from what is shown. Never types a made-up address."""
    desk = ctx.desk
    home = f"https://{domain}"
    host = urlparse(home).hostname or ""
    if web.blocked(home) or not await asyncio.to_thread(web._public_host, host):
        return None
    pages: list[tuple[str, str]] = []

    async def read(label: str) -> tuple[str, str | None]:
        text, why = await _read_checked(ctx, label)
        if why:
            web.bot_blocked.add(domain)  # the lead is kept, flagged for manual contact
            _rest(ctx, domain, why)
        return text, why

    await desk.navigate(home)
    text, why = await read(f"home {domain}")
    if why or _page_missing(desk, await desk.title(), text):
        return None
    pages.append((home, text))
    tried = {home}
    for label in _CONTACT_LABELS:
        if len(pages) >= _MAX_CONTACT_PAGES or contacts.emails_on_domain(pages[-1][1], domain):
            break
        for key in (
            "Home",
            "End",
        ):  # header links first, then the footer, like a person scrolling down
            await desk.act(action="key", key=key)
            await asyncio.sleep(1)
            if await desk.click_link(*label):
                break
        else:
            continue  # no such link on this page: do not guess a URL
        url = await desk.current_url()
        if (
            not url
            or url in tried
            or urlparse(url).hostname not in (host, f"www.{host}", host.removeprefix("www."))
        ):
            await desk.back()
            continue
        tried.add(url)
        text, why = await read(f"contact {domain} {label[0]}")
        if why:
            break
        if _page_missing(desk, await desk.title(), text):
            await desk.back()
            continue
        pages.append((url, text))
        if not contacts.emails_on_domain(text, domain):
            await desk.back()
    return await contacts.pick_contact(domain, pages) if pages else None


# --- judging one opened page -----------------------------------------------------------------------


def _signal(url: str, title: str, text: str) -> sources.Signal:
    sig = sources.Signal(kind="web_page", source="desktop", url=url, title=title, text=text[:6000])
    sig.hash = hashlib.sha256(
        url.encode()
    ).hexdigest()  # one page, one signal, whatever its text says today
    return sig


async def evaluate(ctx: Ctx, hit: search.Hit, url: str, text: str, query: str, engine: str) -> str:  # noqa: PLR0911, PLR0912, PLR0915, PLR0917
    """Judge the opened page and store a lead if it is a real, relevant project. Returns the outcome word."""
    st = ctx.stats
    canon = leadscore.canonical_url(url)
    if canon in ctx.state.visited or await store.signal_seen(
        hashlib.sha256(canon.encode()).hexdigest()
    ):
        st["duplicates"] += 1
        log.info("Duplicate detected", extra={"ctx": {"why": "page already seen", "url": canon}})
        return "duplicate"
    ctx.state.visited.append(canon)
    sig = _signal(canon, hit.title, text)

    async def reject(why: str, counter: str, **extra) -> str:
        st[counter] += 1
        log.info("Lead rejected", extra={"ctx": {"why": why, "url": canon, **extra}})
        await store.record_signal(sig, {"rejected": True, "why": why, **extra})
        return "rejected"

    if leadscore.is_job_posting(text, canon, hit.title):
        return await reject("job posting, not a project", "rejected_job")
    if why := leadscore.exclusion_reason(
        f"{hit.title} {text[:3000]}", urlparse(canon).hostname or "", "",
        ctx.cfg.get("exclude_terms") or [], ctx.cfg.get("exclude_companies") or [],
    ):  # fmt: skip
        return await reject(why, "rejected_irrelevant")
    kws = sum(1 for k in ctx.keywords if sources.matches(text, [k]))
    dev = sum(1 for k in ctx.cfg.get("product_keywords") or [] if sources.matches(text, [k]))
    if kws + dev < max(1, ctx.min_keywords):
        return await reject(
            f"only {kws + dev} platform / device keyword(s), need {ctx.min_keywords}",
            "rejected_irrelevant",
        )
    if sources.is_stale(sig, ctx.max_age_days):
        return await reject("older than max_age_days", "rejected_irrelevant")

    q, problems = await qualify.qualify_signal(
        f"Page: {canon}\nTitle: {hit.title}\n\n{text[:6500]}", "device"
    )
    name = (q.company_name or "").strip()
    log.info(
        "Opportunity evaluated",
        extra={"ctx": {"relevant": q.relevant, "confidence": q.confidence, "company": name}},
    )
    if why := leadscore.country_excluded(
        urlparse(canon).hostname or "", q.location, ctx.cfg.get("exclude_countries") or [],
        ctx.cfg.get("exclude_tlds") or [],
    ):  # fmt: skip
        return await reject(why, "rejected_irrelevant", company=name)
    if why := leadscore.exclusion_reason("", "", name, [], ctx.cfg.get("exclude_companies") or []):
        return await reject(why, "rejected_irrelevant", company=name)
    if not q.relevant or problems or not name:
        return await reject(f"not a qualified project: {q.reason[:120]}", "rejected_irrelevant")
    if q.confidence < ctx.min_confidence:
        return await reject(
            f"model confidence {q.confidence:.2f} below {ctx.min_confidence}: {q.reason[:100]}",
            "rejected_irrelevant",
        )
    st["qualified"] += 1

    domain = ""
    for cand in (q.website, domain_from_page(canon, name)):
        d = web.registrable_domain(cand) if cand else ""
        if web.is_company_site(d) and not web.blocked(d):
            domain = d
            break
    if not domain:
        domain = await find_website(ctx, name)
    if not domain:
        return await reject("company website not found", "rejected_irrelevant", company=name)
    if why := leadscore.country_excluded(domain, "", [], ctx.cfg.get("exclude_tlds") or []):
        return await reject(why, "rejected_irrelevant", company=name)
    if (
        domain in ctx.domains_seen
        or await store.domain_known(domain)
        or await store.company_name_known(name)
    ):
        st["duplicates"] += 1
        log.info(
            "Duplicate detected", extra={"ctx": {"why": "company already stored", "company": name}}
        )
        await store.record_signal(sig, {"duplicate_company": name, "domain": domain})
        return "duplicate"

    own = bool(domain_from_page(canon, name))
    vendor = leadscore.vendor_hits(text)
    args: dict[str, Any] = {"confidence": q.confidence, "vendor": vendor, "keyword_hits": kws, "text_len": len(text),
            "has_summary": bool(q.project_summary), "own_site": own, "product_hits": dev}  # fmt: skip
    pre, _ = leadscore.score_lead(contact=False, **args)
    if pre < ctx.min_score - 10:
        return await reject(f"score {pre} too low", "rejected_score", company=name, score=pre)

    found = await visible_contact(ctx, domain)
    contact, contact_url, c_problems = found if found else (None, "", [])
    score, parts = leadscore.score_lead(contact=bool(contact), **args)
    if score < ctx.min_score:
        return await reject(
            f"score {score} below {ctx.min_score}", "rejected_score", company=name, score=score
        )

    ctx.domains_seen.add(domain)
    qs = SimpleNamespace(confidence=score / 100, reason=f"score {score}/100: {q.reason}"[:400], location=q.location,
                         technologies=q.technologies, project_summary=q.project_summary)  # fmt: skip
    cid = await store.save_company(
        name=name, domain=domain, status="contact_found" if contact else "qualified", q=qs,
        source="desktop", source_url=canon, review=bool(c_problems) or not contact,
        form_url=contacts.form_urls.get(domain), manual_reason=contacts.manual_reason(domain),
    )  # fmt: skip
    record = lead_record(q=q, hit_title=hit.title, url=canon, domain=domain, text=text, score=score,
                         parts=parts, contact=contact, keywords=ctx.keywords, query=query, engine=engine)  # fmt: skip
    await store.record_signal(sig, record, cid)
    st["stored"] += 1
    log.info(
        "Lead stored",
        extra={
            "ctx": {"company": name, "domain": domain, "score": score, "contact": bool(contact)}
        },
    )
    if contact:
        await _draft(q, sig, contact, contact_url, c_problems, cid, st)
    return "stored"


async def _draft(q, sig, contact, contact_url: str, c_problems: list[str], cid, st: dict) -> None:  # noqa: PLR0917
    """Contact saved, proposal drafted and kept as a draft. Nothing is sent: sending is locked off."""
    try:
        contact_id = await store.save_contact(cid, contact, contact_url)
        draft, d_problems = await proposal.draft_proposal(
            lead_pipeline._lead_context(q, sig, contact)
        )
        note = "; ".join([*c_problems, *d_problems]) or None
        if email_id := await store.save_email_draft(contact_id, draft.subject, draft.body, note):
            st["drafts"] += 1
            await notify.post_email(email_id)  # Slack approval request only
    except Exception:
        log.exception("draft failed; the lead is kept without one")


async def _back_to_results(ctx: Ctx, opened_url: str, results_url: str) -> None:
    """Return to the search results: Back if the window is still on the opened page, else retype the address."""
    desk = ctx.desk
    now = await desk.current_url()
    if now == opened_url:
        await desk.back()
    elif now != results_url:
        await desk.navigate(results_url)


async def process_hit(ctx: Ctx, hit: search.Hit, results_url: str, query: str, engine: str) -> None:
    desk, st = ctx.desk, ctx.stats
    log.info(
        "Result discovered",
        extra={"ctx": {"title": hit.title[:80], "domain": hit.domain, "score": hit.score}},
    )
    url = await desk.open_link(hit.title, results_url)
    if not url:
        st["open_failed"] += 1
        log.info("could not open result", extra={"ctx": {"title": hit.title[:80]}})
        return
    log.info("Result opened", extra={"ctx": {"url": url}})
    if not Policy.url_allowed(url):
        log.info(
            "Lead rejected", extra={"ctx": {"why": "portal that must not be visited", "url": url}}
        )
        await _back_to_results(ctx, url, results_url)
        return
    try:
        text, why = await _read_checked(ctx, f"page {hit.title}")
        st["pages_inspected"] += 1
        if why:
            host = urlparse(url).hostname or url
            web.bot_blocked.add(host)
            _rest(ctx, host, why)
            return
        await desk.scroll(2)
        await evaluate(ctx, hit, url, text, query, engine)
    finally:
        await _back_to_results(ctx, url, results_url)


async def process_query(ctx: Ctx, platform: str, query: str, queue: deque, limit_left: int) -> int:  # noqa: PLR0912
    """One search: returns how many leads it stored."""
    st = ctx.stats
    before = st["stored"]
    got = await search_page(ctx, query, st["searches"])
    if got is None:
        log.info("no search engine available right now", extra={"ctx": {"query": query}})
        return 0
    text, results_url, engine = got
    st["searches"] += 1
    read = await search.read_results(text[:9000], query)
    log.info(
        "Query executed",
        extra={
            "ctx": {"query": query, "platform": platform, "engine": engine, "hits": len(read.hits)}
        },
    )
    known = [k.split("|", 1)[-1] for k in ctx.state.done] + [q for _, q in queue]
    cfg = ctx.cfg
    for nq in read.next_queries:  # follow-ups chosen from what the results showed
        if not leadscore.query_on_topic(
            nq, cfg.get("query_topic_terms") or [], cfg.get("query_actor_terms") or [],
            [*(cfg.get("exclude_terms") or []), *(cfg.get("exclude_countries") or [])],
        ):  # fmt: skip
            log.info("Follow-up query dropped (off topic)", extra={"ctx": {"query": nq}})
            continue
        if leadscore.new_query(nq, known):
            queue.appendleft(("web", nq))
            known.append(nq)
    chosen = sorted(
        (h for h in read.hits if h.likely_project and h.score >= ctx.min_hit_score),
        key=lambda h: -h.score,
    )
    seen_titles: list[str] = []
    for hit in chosen:
        if len(seen_titles) >= ctx.hits_per_query or st["stored"] - before >= limit_left:
            break
        if time.monotonic() > ctx.deadline or _killed():
            break
        dom = hit.domain.lower()
        if (
            dom
            and (
                web.blocked(dom)
                or dom in ctx.blocked
                or web.registrable_domain(dom) in leadscore.INDIVIDUAL_HIRING_SITES
            )
        ) or any(leadscore.similar(hit.title, t) for t in seen_titles):
            continue
        if leadscore.looks_like_job(hit.title, dom):
            log.info("Result skipped", extra={"ctx": {"why": "job ad", "title": hit.title[:80]}})
            continue
        if why := leadscore.country_excluded(
            dom, "", [], ctx.cfg.get("exclude_tlds") or []
        ):  # fmt: skip
            log.info("Result skipped", extra={"ctx": {"why": why, "title": hit.title[:80]}})
            continue
        if why := leadscore.exclusion_reason(
            f"{hit.title} {hit.snippet}", dom, "", ctx.cfg.get("exclude_terms") or [],
            ctx.cfg.get("exclude_companies") or [],
        ):  # fmt: skip
            log.info("Result skipped", extra={"ctx": {"why": why, "title": hit.title[:80]}})
            continue
        seen_titles.append(hit.title)
        try:
            await process_hit(ctx, hit, results_url, query, engine)
        except (DesktopError, ExecutorError):
            raise
        except Exception:
            st["errors"] += 1
            log.exception("result failed; moving on")
    return st["stored"] - before


def _killed() -> bool:
    return Policy().kill_file.exists()


async def retry_missing_contacts(ctx: Ctx, limit: int | None = None) -> None:
    """Companies saved earlier without a contact get another visible look at their own site. A company that the
    exclusion lists rule out (saved before they existed) is rejected without opening its site."""
    limit = int(env("DESKTOP_RETRY_LIMIT", "1") or 1) if limit is None else limit
    visited = 0
    for co in await store.companies_without_contact():
        if visited >= limit or time.monotonic() > ctx.deadline or _killed():
            return
        if leadscore.looks_like_job("", co["source_url"] or ""):
            await store.reject_company(
                co["id"]
            )  # stored from a job ad before the job filters existed
            log.info(
                "Stored company dropped",
                extra={"ctx": {"company": co["name"], "why": "found via a job ad"}},
            )
            continue
        if why := leadscore.exclusion_reason(
            co["project_summary"] or "", co["domain"] or "", co["name"] or "",
            ctx.cfg.get("exclude_terms") or [], ctx.cfg.get("exclude_companies") or [],
        ) or leadscore.country_excluded(
            co["domain"] or "", co["location"] or "", ctx.cfg.get("exclude_countries") or [],
            ctx.cfg.get("exclude_tlds") or [],
        ):  # fmt: skip
            await store.reject_company(co["id"])
            log.info("Stored company dropped", extra={"ctx": {"company": co["name"], "why": why}})
            continue
        visited += 1
        log.info(
            "Contact retry",
            extra={
                "ctx": {
                    "company": co["name"],
                    "domain": co["domain"],
                    "location": co["location"],
                    "found_via": co["source_url"],
                }
            },
        )
        try:
            found = await visible_contact(ctx, co["domain"])
            if not found:
                await store.touch_company(co["id"])
                continue
            contact, url, c_problems = found
            await store.mark_contact_found(co["id"])
            q = SimpleNamespace(company_name=co["name"], project_summary=co["project_summary"] or "",
                                technologies=co["technologies"] or [], location=co["location"] or "")  # fmt: skip
            sig = SimpleNamespace(source=co["source"] or "", url=co["source_url"] or "")
            await _draft(q, sig, contact, url, c_problems, co["id"], ctx.stats)
        except (DesktopError, ExecutorError):
            raise
        except Exception:
            ctx.stats["errors"] += 1
            log.exception("contact retry failed")


# --- the run ---------------------------------------------------------------------------------------


async def run(deadline: float) -> dict:  # noqa: PLR0915
    """Search until the daily target is stored, the search space is used up, or `deadline` (time.monotonic())."""
    mailer.lock_sending()
    log.info("Email sending disabled", extra={"ctx": {"locked": True}})
    if not shutil.which("nvidia-smi"):
        # No GPU: the models run on the CPU, a call takes tens of seconds. Give every call time to finish.
        os.environ.setdefault("LLM_MIN_TIMEOUT", "300")
        log.info("No NVIDIA GPU: models run on the CPU, call time limits raised")
    cfg = settings.load()
    conf = sources.load_config()
    dcfg = conf.get("desktop") or {}
    stats = {"searches": 0, "pages_inspected": 0, "qualified": 0, "stored": 0, "drafts": 0, "duplicates": 0,
             "blocked_sources": 0, "rejected_job": 0, "rejected_irrelevant": 0, "rejected_score": 0,
             "open_failed": 0, "errors": 0, "browser_recoveries": 0, "emails_sent": 0}  # fmt: skip
    problems = [
        f"{name}: {detail}" for ok, name, detail, required in preflight() if required and not ok
    ]
    if problems:
        log.error("desktop is not ready", extra={"ctx": {"problems": problems}})
        return {**stats, "error": "; ".join(problems)}
    stop_at_target = (env("DESKTOP_STOP_AT_TARGET", "true") or "true").lower() != "false"
    limit = cfg.lead_target if stop_at_target else cfg.lead_max
    limit = min(limit, cfg.lead_max)
    already = await store.desktop_leads_today()
    if already >= limit:
        log.info(
            "daily lead target already reached",
            extra={"ctx": {"stored_today": already, "target": limit}},
        )
        return {**stats, "skipped": "target already reached", "stored_today": already}

    started = time.monotonic()
    run_id = uuid.uuid4().hex[:8]
    state = State.load(Path(env("DESKTOP_STATE_FILE", "out/desktop/state.json") or ""))
    desk = Desktop(run_id)
    ctx = Ctx(desk=desk, cfg=dcfg, keywords=conf.get("keywords") or [], max_age_days=int(conf.get("max_age_days", 45)),
              state=state, deadline=deadline, stats=stats,
              min_score=int(env("DESKTOP_MIN_SCORE", "60") or 60),
              hits_per_query=int(env("DESKTOP_HITS_PER_QUERY", "4") or 4),
              min_hit_score=float(env("DESKTOP_MIN_HIT_SCORE", "0.4") or 0.4),
              min_keywords=int(env("DESKTOP_MIN_KEYWORDS", "2") or 2),
              min_confidence=float(env("DESKTOP_MIN_CONFIDENCE", "0.7") or 0.7))  # fmt: skip
    queue = build_queue(dcfg)
    queue = deque(item for item in queue if f"{item[0]}|{item[1]}" not in state.done)
    gap = float(env("DESKTOP_SEARCH_GAP", "8") or 8)
    log.info(
        "Agent started",
        extra={
            "ctx": {"run": run_id, "target": limit, "stored_today": already, "queries": len(queue)}
        },
    )
    consecutive = 0
    try:
        await desk.start()
        log.info("Browser initialized", extra={"ctx": {"screen": desk.screen}})
        await retry_missing_contacts(ctx)
        while queue and time.monotonic() < deadline and not _killed():
            left = limit - already - stats["stored"]
            if left <= 0:
                log.info("daily target reached", extra={"ctx": {"stored": stats["stored"]}})
                break
            platform, query = queue.popleft()
            try:
                await process_query(ctx, platform, query, queue, left)
                state.done.append(f"{platform}|{query}")
                consecutive = 0
            except (DesktopError, ExecutorError) as exc:
                stats["errors"] += 1
                consecutive += 1
                log.warning(
                    "browser problem",
                    extra={"ctx": {"error": str(exc)[:160], "consecutive": consecutive}},
                )
                queue.append((platform, query))  # try this search again later
                if consecutive > 4:
                    log.exception("giving up: the browser keeps failing")
                    break
                await desk.recover()
                stats["browser_recoveries"] = desk.recoveries
                log.info("Browser recovered", extra={"ctx": {"count": desk.recoveries}})
            except Exception:
                stats["errors"] += 1
                consecutive += 1
                log.exception("search failed; moving on")
                state.done.append(
                    f"{platform}|{query}"
                )  # not retried: something about this query is off
            state.save()
            if all(state.rest_until.get(e, 0) > time.time() for e in _engines(ctx)):
                wake = min(state.rest_until.get(e, 0) for e in _engines(ctx))
                if wake - time.time() > deadline - time.monotonic():
                    log.warning("every search engine is blocked for the rest of this run")
                    break
                await asyncio.sleep(min(max(wake - time.time(), 5), 300))
            else:
                await asyncio.sleep(gap)
    finally:
        state.save()
        stats["browser_recoveries"] = desk.recoveries
        stats["run_seconds"] = round(time.monotonic() - started)
        stats["stored_today"] = already + stats["stored"]
        log.info("Run completed", extra={"ctx": stats})
    return stats
