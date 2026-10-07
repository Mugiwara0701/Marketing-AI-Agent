"""Postgres integration: the real migrations, decide_email() and the sending gate in SQL.

Needs a throwaway database (never production): TEST_DATABASE_URL=postgresql://postgres:test@127.0.0.1:55432/postgres
    docker run -d --rm --name leadgen-pg-test -e POSTGRES_PASSWORD=test -p 127.0.0.1:55432:5432 pgvector/pgvector:pg16
Skipped when TEST_DATABASE_URL is not set."""

import asyncio
import os
from pathlib import Path

import pytest

from agent.leadgen import approval, identity, sender
from agent.leadgen.models import Contact, Evidence, Lead, LeadStatus, ScoreCard
from agentkit import db

URL = os.environ.get("TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not URL, reason="set TEST_DATABASE_URL to a throwaway Postgres")

_SUPABASE_STUBS = """
do $$ begin create role authenticated; exception when duplicate_object then null; end $$;
create schema if not exists auth;
create or replace function auth.uid() returns uuid language sql stable as $$ select null::uuid $$;
"""


def _run(coro):
    """One event loop per call, so the pool is closed inside it (asyncpg pools belong to their loop)."""

    async def go():
        try:
            return await coro
        finally:
            await db.close_pool()

    return asyncio.run(go())


def _apply(conn_sql: list[str]) -> None:
    async def go():
        pool = await db.get_pool()
        async with pool.acquire() as conn:
            for sql in conn_sql:
                await conn.execute(sql)

    _run(go())


@pytest.fixture(autouse=True)
def _db_url(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", URL)  # conftest removes it before every test


@pytest.fixture(scope="module")
def pg():
    os.environ["DATABASE_URL"] = URL
    files = sorted(Path("supabase/migrations").glob("*.sql"))
    before = [f.read_text() for f in files if f.name < "0007"]
    _apply(["drop schema public cascade; create schema public;", _SUPABASE_STUBS, *before])
    # legacy data from before the lead pipeline: an approved email with nobody recorded as approver
    _apply([
        """insert into campaigns (name, steps) values ('daily-outreach', 2);
           insert into companies (id, name, domain, status) values
             ('11111111-1111-1111-1111-111111111111', 'Old Co Pvt Ltd', 'oldco.com', 'contact_found');
           insert into contacts (id, company_id, email, source_url) values
             ('22222222-2222-2222-2222-222222222222', '11111111-1111-1111-1111-111111111111', 'a@oldco.com', 'u');
           insert into emails (id, contact_id, campaign_id, step, status, subject, body, idempotency_key)
             select '33333333-3333-3333-3333-333333333333', '22222222-2222-2222-2222-222222222222', id, 1,
                    'approved', 's', 'b', 'intro-legacy' from campaigns;""",
        *[f.read_text() for f in files if f.name >= "0007"],
    ])  # fmt: skip
    from agent.leadgen.repository.postgres import PostgresRepository

    return PostgresRepository()


def test_migration_backfills_and_sends_legacy_approvals_back_to_review(pg):
    row = _run(db.fetchrow("select lead_status, name_key from companies where domain='oldco.com'"))
    assert row and row["lead_status"] == "PENDING_APPROVAL" and row["name_key"] == "old"
    e = _run(
        db.fetchrow("select status from emails where id='33333333-3333-3333-3333-333333333333'")
    )
    assert e and e["status"] == "drafted"  # nobody recorded as approver: back to review


@pytest.mark.parametrize(
    "name", ["Acme EV Technologies Pvt. Ltd.", "Technologies Ltd", "The ABB Group", "Ünï Co"]
)
def test_sql_name_key_matches_python(pg, name):
    row = _run(db.fetchrow("select lead_name_key($1) k", name))
    assert row and row["k"] == identity.name_key(name)


def test_full_flow_and_gate_in_postgres(pg, monkeypatch):
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.delenv("TEST_RECIPIENT", raising=False)

    async def go():
        lead = Lead(company_name="VoltGrid Energy", company_website="voltgrid.com", name_key="voltgrid",
                    industry="EV charging", technical_requirements=["embedded_linux"], lead_score=72,
                    score=ScoreCard(total=72, parts={"project_signal": 8}),
                    evidence=[Evidence(url="https://voltgrid.com", reason="Linux device", source="llm")],
                    source_urls=["https://voltgrid.com"])  # fmt: skip
        lid = await pg.insert_lead(lead, source="test")
        assert not await pg.set_status(lid, LeadStatus.APPROVED)  # cannot skip the approval
        for s in (LeadStatus.QUALIFIED, LeadStatus.CONTACT_FOUND, LeadStatus.EMAIL_DRAFTED):
            assert await pg.set_status(lid, s)
        cid = await pg.save_contact(
            lid, Contact(name="Ana", role="CTO", email="Ana@VoltGrid.com", source="u", rank=0)
        )
        eid = await pg.save_email_draft(lid, cid, "Hello", "Body", None)
        assert (
            eid and await pg.save_email_draft(lid, cid, "x", "y", None) is None
        )  # one intro per contact
        got = await pg.find_lead(name_key="voltgrid")
        assert (
            got
            and got.contact
            and got.contact.email == "ana@voltgrid.com"
            and got.evidence[0].source == "llm"
        )
        await pg.open_approval(eid, "#c")
        await pg.open_approval(eid, "#c")  # idempotent
        await pg.set_approval_message(eid, "#c", "171.2")
        assert await pg.sendable_problem(eid) == "email status is drafted, not approved"
        assert await pg.claim_sendable(10) == []
        assert await approval.decide(pg, eid, True, "slack:U1 (Ana)") == "approved"
        assert await approval.decide(pg, eid, False, "slack:U2") == "already_decided"
        ap = await pg.get_approval(eid)
        assert (
            ap
            and ap.approved_by == "slack:U1 (Ana)"
            and ap.slack_message_id == "171.2"
            and ap.approved_at
        )
        assert (await pg.get_lead(lid)).status == LeadStatus.APPROVED
        legacy = "33333333-3333-3333-3333-333333333333"
        assert await pg.sendable_problem(legacy) is not None

        sent: list[str] = []

        class T:
            name = "test"

            async def send(self, item):
                sender._check(item)
                sent.append(item.email.email_id)
                return sender.Sent("<m@x>", "pid")

        os.environ.setdefault("MAIL_FROM", "from@x.io")
        stats = await sender.send_approved(pg, T(), gap_seconds=0)
        assert stats["sent"] == 1 and sent == [eid]
        assert (await pg.get_lead(lid)).status == LeadStatus.SENT
        assert (await pg.get_email(eid)).status == "sent"
        assert (await sender.send_approved(pg, T(), gap_seconds=0))["sent"] == 0  # never twice
        legacy_status = await db.fetchrow(
            "select company_id from contacts where id='22222222-2222-2222-2222-222222222222'"
        )
        assert legacy_status  # untouched

    _run(go())


def test_rejection_in_postgres(pg):
    async def go():
        lid = await pg.insert_lead(
            Lead(company_name="Kioskly", company_website="kioskly.io"), source="t"
        )
        for s in (LeadStatus.QUALIFIED, LeadStatus.CONTACT_FOUND, LeadStatus.EMAIL_DRAFTED):
            await pg.set_status(lid, s)
        cid = await pg.save_contact(lid, Contact(email="info@kioskly.io", source="u"))
        eid = await pg.save_email_draft(lid, cid, "s", "b", None)
        assert await approval.decide(pg, eid, False, "cli:me") == "rejected"
        assert (await pg.get_lead(lid)).status == LeadStatus.REJECTED
        assert (await pg.get_email(eid)).status == "skipped"
        assert await pg.sendable_problem(eid) == "email status is skipped, not approved"
        assert (
            await approval.decide(pg, "44444444-4444-4444-4444-444444444444", True, "x")
            == "unknown"
        )

    _run(go())


def test_contact_attempts_in_postgres(pg):
    async def go():
        lid = await pg.insert_lead(
            Lead(company_name="Retry Co", company_website="retryco.io"), source="t"
        )
        await pg.set_status(lid, LeadStatus.QUALIFIED)
        assert lid in [x.lead_id for x in await pg.leads_needing_contact(2, 50)]
        assert await pg.record_contact_attempt(lid) == 1
        assert await pg.record_contact_attempt(lid) == 2
        assert lid not in [x.lead_id for x in await pg.leads_needing_contact(2, 50)]

    _run(go())


def test_result_type_and_tier_are_stored_in_postgres(pg):
    async def go():
        lead = Lead(company_name="Tier Co", company_website="tierco.io", result_type="POTENTIAL_CUSTOMER",
                    customer_tier="high")  # fmt: skip
        lid = await pg.insert_lead(lead, source="t")
        got = await pg.get_lead(lid)
        assert got and (got.result_type, got.customer_tier) == ("POTENTIAL_CUSTOMER", "high")
        got.customer_tier = "potential"
        await pg.save_lead(got)
        again = await pg.get_lead(lid)
        assert again and again.customer_tier == "potential"

    _run(go())


def test_gmail_ids_and_thread_continuation_in_postgres(pg):
    async def go():
        lid = await pg.insert_lead(
            Lead(company_name="Thread Co", company_website="threadco.io"), source="t"
        )
        for s in (LeadStatus.QUALIFIED, LeadStatus.CONTACT_FOUND, LeadStatus.EMAIL_DRAFTED):
            await pg.set_status(lid, s)
        cid = await pg.save_contact(lid, Contact(email="a@threadco.io", source="u"))
        eid = await pg.save_email_draft(lid, cid, "Hi", "Body", None)
        await pg.decide_email(eid, True, "tester")
        assert await pg.thread_id_for(eid) is None  # an intro starts a conversation
        await db.execute("update emails set status='sending' where id=$1::uuid", eid)
        await pg.mark_sent(eid, "<intro@x>", "gm1", "from@x.io", "th-1")
        row = await db.fetchrow(
            "select gmail_message_id, gmail_thread_id from emails where id=$1::uuid", eid
        )
        assert row and (row["gmail_message_id"], row["gmail_thread_id"]) == ("gm1", "th-1")
        follow = await db.fetchrow(
            """insert into emails (contact_id, campaign_id, step, status, subject, body, in_reply_to, idempotency_key)
               select $1::uuid, campaign_id, 2, 'drafted', 'Re: Hi', 'B', '<intro@x>', 'f-1' from emails
                where id=$2::uuid returning id""",
            cid, eid,
        )  # fmt: skip
        assert (
            follow and await pg.thread_id_for(str(follow["id"])) == "th-1"
        )  # stays in the intro's thread

    _run(go())


def test_pipeline_control_row_round_trip(pg):
    """Migration 0011: the dashboard's request, the agent's report and heartbeat, offline on shutdown."""
    import asyncpg

    from agent import control

    store = control.PostgresControl(host="office")

    async def go():
        fresh = await store.load()
        asked = await control.request("running", "dashboard:akshat")
        loaded = await store.load()
        p = control.Pipeline(rest_minutes=30)
        p.running, p.step, p.last = (
            True,
            "leads",
            {"outcome": "stopped", "result": {"leads": {"searches": 3}}},
        )
        await store.report(p.status())
        reported = await control.read()
        again = await store.load()
        await store.offline()
        off = await control.read()
        try:
            await control.request("paused", "x")
            bad = None
        except asyncpg.PostgresError as exc:
            bad = exc
        return fresh, asked, loaded, reported, again, off, bad

    fresh, asked, loaded, reported, again, off, bad = _run(go())
    assert (
        fresh["desired_state"] == "stopped" and fresh["last_pass"] is None
    )  # a fresh install is stopped
    assert asked["desired_state"] == "running" and asked["requested_by"] == "dashboard:akshat"
    assert loaded["desired_state"] == "running"
    assert reported["actual_state"] == "running" and reported["current_step"] == "leads"
    assert reported["heartbeat_at"] is not None and reported["agent_host"] == "office"
    assert again["last_pass"] == {"outcome": "stopped", "result": {"leads": {"searches": 3}}}
    assert (
        off["heartbeat_at"] is None
        and off["actual_state"] == "stopped"
        and off["desired_state"] == "running"
    )
    assert bad is not None and "running or stopped" in str(bad)
