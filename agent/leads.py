"""Part 1: find companies that need AOSP / embedded engineering work, store them, draft proposals."""

import time
from types import SimpleNamespace

from agentkit.log import get_logger

from . import contacts, notify, settings, sources, store, web
from .tasks import proposal, qualify

log = get_logger("agent.leads")

_MAX_CONTACT_LOOKUPS = 40


def _lead_context(q, signal, contact) -> str:
    return (
        f"Company: {q.company_name}\n"
        f"Project: {q.project_summary}\n"
        f"Technologies: {', '.join(q.technologies) or 'not stated'}\n"
        f"Location: {q.location or 'not stated'}\n"
        f"Source: {signal.source} ({signal.url})\n"
        f"Contact name: {contact.name or 'unknown'}\n"
        f"Contact role: {contact.role or 'business contact'}\n"
    )


async def _retry_without_contact(slots: int, deadline: float, stats: dict) -> int:
    """Second look at saved companies that had no contact. Returns the remaining lead slots."""
    for co in await store.companies_without_contact():
        if slots <= 0 or time.monotonic() > deadline:
            break
        try:
            found = await contacts.find_contact(co["domain"])
            if not found:
                if form := contacts.form_urls.get(co["domain"]):
                    await store.set_form_url(co["id"], form)
                if why := contacts.manual_reason(co["domain"]):
                    await store.set_manual_reason(co["id"], why)
                await store.touch_company(co["id"])  # go to the back of the queue
                continue
            contact, url, c_problems = found
            q = SimpleNamespace(
                company_name=co["name"],
                project_summary=co["project_summary"] or "",
                technologies=co["technologies"] or [],
                location=co["location"] or "",
            )
            sig = SimpleNamespace(source=co["source"] or "", url=co["source_url"] or "")
            await store.mark_contact_found(co["id"])
            contact_id = await store.save_contact(co["id"], contact, url)
            draft, d_problems = await proposal.draft_proposal(_lead_context(q, sig, contact))
            note = "; ".join([*c_problems, *d_problems]) or None
            email_id = await store.save_email_draft(contact_id, draft.subject, draft.body, note)
            if email_id:
                stats["drafted"] += 1
                await notify.post_email(email_id)
            stats["leads"] += 1
            slots -= 1
        except Exception:
            stats["failed"] += 1
            log.exception("retry failed")
    return slots


async def run(deadline: float) -> dict:  # noqa: PLR0912, PLR0915
    """deadline: time.monotonic() value after which no new work starts."""
    if settings.desktop_mode():  # visible Chrome on the desktop instead of the HTTP sources below
        from .gui import discover  # noqa: PLC0415

        return await discover.run(deadline)
    cfg = settings.load()
    slots = cfg.max_new_leads - await store.new_leads_today()
    stats = {"processed": 0, "failed": 0, "signals": 0, "qualified": 0, "leads": 0, "drafted": 0}
    if slots <= 0:
        log.info("daily lead cap already reached")
        return stats
    slots = await _retry_without_contact(slots, deadline, stats)
    if slots <= 0:
        return stats
    signals = await sources.collect_signals(cfg.max_signals)
    stats["signals"] = len(signals)
    lookups = 0
    done_domains: set[str] = set()  # one lead per company per run, even with many postings
    for sig in signals:
        if slots <= 0 or time.monotonic() > deadline or lookups >= _MAX_CONTACT_LOOKUPS:
            break
        try:
            if await store.signal_seen(sig.hash):
                continue
            if sig.domain_hint and sig.domain_hint in done_domains:
                continue
            stats["processed"] += 1
            q, problems = await qualify.qualify_signal(sig.text)
            if not q.relevant or problems or not (q.company_name or sig.company_hint):
                await store.record_signal(
                    sig, {**q.model_dump(), "problems": problems, "rejected": True}
                )
                continue
            stats["qualified"] += 1
            name = q.company_name or sig.company_hint
            domain = (
                web.registrable_domain(sig.domain_hint or q.website)
                if (sig.domain_hint or q.website)
                else ""
            )
            if not web.is_company_site(domain):
                domain = await sources.resolve_website(name)
            if not domain or await store.domain_known(domain):
                await store.record_signal(
                    sig, {**q.model_dump(), "rejected": True, "why": "no website or already known"}
                )
                continue
            lookups += 1
            done_domains.add(domain)
            found = await contacts.find_contact(domain)
            if not found:
                cid = await store.save_company(
                    name=name,
                    domain=domain,
                    status="qualified",
                    q=q,
                    source=sig.source,
                    source_url=sig.url,
                    review=True,
                    form_url=contacts.form_urls.get(domain),
                    manual_reason=contacts.manual_reason(domain),
                )
                await store.record_signal(sig, q.model_dump(), cid)
                continue
            contact, contact_url, c_problems = found
            cid = await store.save_company(
                name=name,
                domain=domain,
                status="contact_found",
                q=q,
                source=sig.source,
                source_url=sig.url,
                review=bool(c_problems),
            )
            await store.record_signal(sig, q.model_dump(), cid)
            contact_id = await store.save_contact(cid, contact, contact_url)
            draft, d_problems = await proposal.draft_proposal(_lead_context(q, sig, contact))
            note = "; ".join([*c_problems, *d_problems]) or None
            email_id = await store.save_email_draft(contact_id, draft.subject, draft.body, note)
            if email_id:
                stats["drafted"] += 1
                await notify.post_email(email_id)
            stats["leads"] += 1
            slots -= 1
        except Exception:
            stats["failed"] += 1
            log.exception("lead failed")  # no PII: only the exception is logged
    log.info("leads done", extra={"ctx": stats})
    return stats
