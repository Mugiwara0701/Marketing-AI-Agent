"""The one-off check of stored lead websites: dead sites are rejected with --apply, mismatches only reported."""

import asyncio

from agent.leadgen import domain_check
from agent.leadgen.models import Contact, Lead, LeadStatus
from agent.leadgen.repository.sqlite import SqliteRepository


async def _lead(repo, name, domain, found_on, *, status=LeadStatus.QUALIFIED, draft=False):
    lid = await repo.insert_lead(
        Lead(company_name=name, company_website=domain, source_urls=[found_on]), source="old-run"
    )
    for s in (LeadStatus.QUALIFIED, LeadStatus.CONTACT_FOUND, LeadStatus.EMAIL_DRAFTED):
        await repo.set_status(lid, s)
        if s == status:
            break
    if draft:
        cid = await repo.save_contact(lid, Contact(email=f"info@{domain}", source="u"))
        await repo.save_email_draft(lid, cid, "s", "b", None)
    return lid


def _fake_probe(monkeypatch, answers):
    async def probe(domain, timeout=12):
        return answers[domain]

    monkeypatch.setattr(domain_check, "probe", probe)


def test_report_only_by_default_then_apply(monkeypatch):
    repo = SqliteRepository(":memory:")
    _fake_probe(monkeypatch, {
        "siliconsignals.com": (False, "RemoteProtocolError"),
        "acme.com": (True, "acme.com"),
        "other-brand.com": (True, "other-brand.com"),
        "deaddraft.com": (False, "ConnectError"),
    })  # fmt: skip
    dead = asyncio.run(_lead(repo, "Silicon Signals", "siliconsignals.com",
                             "https://siliconsignals.io/blog/a-guide-to-hal"))  # fmt: skip
    ok = asyncio.run(
        _lead(repo, "Acme", "acme.com", "https://www.indeed.com/job/1")
    )  # a job board: not compared
    sus = asyncio.run(_lead(repo, "Brand", "other-brand.com", "https://brand.io/products"))
    drafted = asyncio.run(_lead(repo, "Draft Co", "deaddraft.com", "https://deaddraft.io/",
                                status=LeadStatus.EMAIL_DRAFTED, draft=True))  # fmt: skip

    found = {f.lead_id: f for f in asyncio.run(domain_check.run(repo))}
    assert (
        found[dead].status == "dead" and found[ok].status == "ok" and found[sus].status == "suspect"
    )
    assert asyncio.run(repo.get_lead(dead)).status == LeadStatus.QUALIFIED  # type: ignore[union-attr]

    asyncio.run(domain_check.run(repo, apply=True))
    assert asyncio.run(repo.get_lead(dead)).status == LeadStatus.REJECTED  # type: ignore[union-attr]
    assert asyncio.run(repo.get_lead(sus)).status == LeadStatus.QUALIFIED  # type: ignore[union-attr]
    lead = asyncio.run(repo.get_lead(drafted))
    assert lead and lead.status == LeadStatus.REJECTED and lead.email_draft_id
    ap = asyncio.run(repo.get_approval(lead.email_draft_id))
    assert ap and ap.status == "rejected" and ap.rejected_by == domain_check.BY
