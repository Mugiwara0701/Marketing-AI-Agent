"""The hard requirement: no email leaves without a recorded human approval. Also the lead state machine.

Runs against the real SQLite repository (the same contract the Postgres one implements in SQL)."""

import asyncio

import pytest

from agent.leadgen import approval, sender
from agent.leadgen.models import (
    Contact,
    EmailDraft,
    InvalidTransitionError,
    Lead,
    LeadStatus,
    check_transition,
)
from agent.leadgen.repository.sqlite import SqliteRepository


class RecordingTransport:
    name = "recording"

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, item):
        sender._check(item)
        self.sent.append(item.email.email_id)
        return "<msg@test>", "pid"


def _lead_with_draft(repo: SqliteRepository, domain: str = "voltgrid.com") -> tuple[str, str]:
    async def go():
        lead = Lead(
            company_name="VoltGrid", company_website=domain, name_key="voltgrid", lead_score=80
        )
        lid = await repo.insert_lead(lead, source="test")
        for s in (LeadStatus.QUALIFIED, LeadStatus.CONTACT_FOUND, LeadStatus.EMAIL_DRAFTED):
            assert await repo.set_status(lid, s)
        cid = await repo.save_contact(
            lid, Contact(name="Ana", role="CTO", email=f"ana@{domain}", source="u", rank=0)
        )
        eid = await repo.save_email_draft(lid, cid, "Hello", "Body " * 30, None)
        assert eid
        return lid, eid

    return asyncio.run(go())


def _send(repo, transport=None):
    return asyncio.run(sender.send_approved(repo, transport or RecordingTransport(), gap_seconds=0))


@pytest.fixture
def repo(monkeypatch):
    monkeypatch.setenv("APP_ENV", "prod")  # the dev recipient allowlist is tested separately
    monkeypatch.delenv("TEST_RECIPIENT", raising=False)
    r = SqliteRepository(":memory:")
    yield r
    asyncio.run(r.close())


def test_unapproved_email_cannot_be_sent(repo):
    _lid, eid = _lead_with_draft(repo)
    t = RecordingTransport()
    assert _send(repo, t)["sent"] == 0 and t.sent == []
    assert asyncio.run(repo.sendable_problem(eid)) == "email status is drafted, not approved"


def test_rejected_email_cannot_be_sent(repo):
    lid, eid = _lead_with_draft(repo)
    assert asyncio.run(approval.decide(repo, eid, False, "tester")) == "rejected"
    t = RecordingTransport()
    assert _send(repo, t)["sent"] == 0 and t.sent == []
    lead = asyncio.run(repo.get_lead(lid))
    assert lead and lead.status == LeadStatus.REJECTED
    ap = asyncio.run(repo.get_approval(eid))
    assert ap and ap.status == "rejected" and ap.rejected_by == "tester" and ap.rejected_at


def test_unknown_email_cannot_be_sent_or_decided(repo):
    assert asyncio.run(repo.sendable_problem("no-such-id")) == "unknown email"
    assert asyncio.run(approval.decide(repo, "no-such-id", True, "x")) == "unknown"


def test_approved_email_is_sent_once_and_the_lead_moves_to_sent(repo):
    lid, eid = _lead_with_draft(repo)
    asyncio.run(repo.open_approval(eid, "#c"))
    asyncio.run(repo.set_approval_message(eid, "#c", "123.45"))
    assert asyncio.run(approval.decide(repo, eid, True, "slack:U1")) == "approved"
    assert (
        asyncio.run(approval.decide(repo, eid, True, "slack:U2")) == "already_decided"
    )  # exactly once
    ap = asyncio.run(repo.get_approval(eid))
    assert (
        ap
        and ap.status == "approved"
        and ap.approved_by == "slack:U1"
        and ap.slack_message_id == "123.45"
    )
    t = RecordingTransport()
    assert _send(repo, t)["sent"] == 1 and t.sent == [eid]
    assert _send(repo, t)["sent"] == 0  # never twice
    lead = asyncio.run(repo.get_lead(lid))
    assert lead and lead.status == LeadStatus.SENT


def test_approved_email_without_an_approval_record_is_not_sent(repo):
    """Someone flips the email row to 'approved' by hand: no recorded human decision, no mail."""
    lid, eid = _lead_with_draft(repo)
    repo.db.execute("update emails set status='approved' where id=?", (eid,))
    repo.db.execute("update companies set lead_status='APPROVED' where id=?", (lid,))
    t = RecordingTransport()
    assert _send(repo, t)["sent"] == 0 and t.sent == []
    assert asyncio.run(repo.sendable_problem(eid)) == "no recorded human approval"


def test_lead_not_approved_blocks_the_intro_even_with_an_approval_row(repo):
    lid, eid = _lead_with_draft(repo)
    asyncio.run(approval.decide(repo, eid, True, "x"))
    repo.db.execute("update companies set lead_status='REJECTED' where id=?", (lid,))
    assert _send(repo)["sent"] == 0
    assert "not APPROVED" in (asyncio.run(repo.sendable_problem(eid)) or "")


def test_a_transport_refuses_anything_not_cleared_by_the_gate():
    draft = EmailDraft(
        email_id="e", lead_id="l", contact_id="c", to="a@b.io", subject="s", body="b"
    )
    with pytest.raises(sender.NotApprovedError):
        sender.Cleared(email=draft, to="a@b.io", subject="s", token=object())
    with pytest.raises(sender.NotApprovedError):
        asyncio.run(sender.OutboxTransport.__new__(sender.OutboxTransport).send(draft))  # type: ignore[arg-type]
    with pytest.raises(sender.NotApprovedError):
        asyncio.run(sender.ResendTransport().send(draft))  # type: ignore[arg-type]


def test_sending_is_off_unless_enabled(repo, monkeypatch, tmp_path):
    monkeypatch.delenv("EMAIL_SENDING_ENABLED", raising=False)
    assert sender.make_transport(False, tmp_path) is None
    assert asyncio.run(sender.send_approved(repo, None))["disabled"] == 1
    monkeypatch.setenv("EMAIL_SENDING_ENABLED", "true")
    monkeypatch.setenv("EMAIL_SENDING_LOCKED", "1")
    assert sender.make_transport(False, tmp_path) is None  # locked wins
    assert isinstance(
        sender.make_transport(True, tmp_path), sender.OutboxTransport
    )  # dry-run never real


def test_resend_transport_rechecks_the_switch(monkeypatch):
    monkeypatch.delenv("EMAIL_SENDING_ENABLED", raising=False)
    draft = EmailDraft(
        email_id="e", lead_id="l", contact_id="c", to="a@b.io", subject="s", body="b"
    )
    item = sender.Cleared(email=draft, to="a@b.io", subject="s", token=sender._GATE)
    with pytest.raises(sender.NotApprovedError, match="disabled"):
        asyncio.run(sender.ResendTransport().send(item))


def test_dry_run_outbox_writes_and_marks_sent(repo, tmp_path):
    _lid, eid = _lead_with_draft(repo)
    asyncio.run(approval.decide(repo, eid, True, "x"))
    out = _send(repo, sender.OutboxTransport(tmp_path))
    assert out["sent"] == 1
    assert "DRY RUN - NOT SENT" in (tmp_path / "outbox" / f"{eid}.txt").read_text()


def test_suppressed_and_dev_allowlist(repo, monkeypatch):
    _lid, eid = _lead_with_draft(repo)
    asyncio.run(approval.decide(repo, eid, True, "x"))
    repo.db.execute("insert into suppression_list values ('ana@voltgrid.com', 'unsubscribe')")
    assert _send(repo)["skipped"] == 1
    _lid2, eid2 = _lead_with_draft(repo, "kioskly.io")
    asyncio.run(approval.decide(repo, eid2, True, "x"))
    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.setenv("ALLOWED_RECIPIENT_DOMAINS", "example.org")
    assert _send(repo)["skipped"] == 1  # dev: only allowlisted recipient domains


def test_test_recipient_redirects(repo, monkeypatch):
    _lid, eid = _lead_with_draft(repo)
    asyncio.run(approval.decide(repo, eid, True, "x"))
    monkeypatch.setenv("TEST_RECIPIENT", "me@test.io")
    seen = []

    class T(RecordingTransport):
        async def send(self, item):
            seen.append((item.to, item.subject))
            return await super().send(item)

    assert _send(repo, T())["sent"] == 1
    assert seen == [("me@test.io", "[TEST for ana@voltgrid.com] Hello")]


def test_send_cap_releases_the_rest(repo, monkeypatch):
    monkeypatch.setenv("DAILY_SEND_CAP_PER_MAILBOX", "1")
    ids = []
    for d in ("a.com", "b.com"):
        _, eid = _lead_with_draft(repo, d)
        asyncio.run(approval.decide(repo, eid, True, "x"))
        ids.append(eid)
    assert _send(repo)["sent"] == 1
    statuses = sorted(asyncio.run(repo.get_email(i)).status for i in ids)
    assert statuses == ["approved", "sent"]  # the second waits for tomorrow, still approved


def test_provider_failure_retries_then_fails_the_lead(repo):
    lid, eid = _lead_with_draft(repo)
    asyncio.run(approval.decide(repo, eid, True, "x"))

    class Broken(RecordingTransport):
        async def send(self, item):
            raise RuntimeError("resend 500")

    for _ in range(sender.MAX_ATTEMPTS):
        assert _send(repo, Broken())["failed"] == 1
    assert asyncio.run(repo.get_email(eid)).status == "expired"
    assert asyncio.run(repo.get_lead(lid)).status == LeadStatus.FAILED


def test_state_machine_transitions(repo):
    check_transition(LeadStatus.DISCOVERED, LeadStatus.QUALIFIED)
    with pytest.raises(InvalidTransitionError):
        check_transition(LeadStatus.DISCOVERED, LeadStatus.SENT)
    with pytest.raises(InvalidTransitionError):
        check_transition(LeadStatus.EMAIL_DRAFTED, LeadStatus.SENT)  # approval cannot be skipped
    lid = asyncio.run(repo.insert_lead(Lead(company_name="X", company_website="x.io"), source="t"))
    assert not asyncio.run(
        repo.set_status(lid, LeadStatus.APPROVED)
    )  # the repository enforces it too
    assert not asyncio.run(repo.set_status(lid, LeadStatus.SENT))
    assert asyncio.run(repo.set_status(lid, LeadStatus.QUALIFIED))
    assert not asyncio.run(
        repo.set_status(lid, LeadStatus.QUALIFIED)
    )  # already there: no double move


def test_approval_request_posts_and_marks_pending(repo, tmp_path):
    lid, eid = _lead_with_draft(repo)
    lead = asyncio.run(repo.get_lead(lid))
    email = asyncio.run(repo.get_email(eid))
    assert lead and email and lead.contact
    sim = approval.SimulatedApprover(tmp_path)
    assert asyncio.run(approval.request(repo, sim, lead, lead.contact, email))
    assert asyncio.run(repo.get_lead(lid)).status == LeadStatus.PENDING_APPROVAL
    msg = (tmp_path / "approvals" / f"{eid}.md").read_text()
    for part in ("Company:", "Industry:", "Opportunity:", "Technical signals:", "Contact:", "Email:",
                 "Why this is a lead:", "Source:", "Approve"):  # fmt: skip
        assert part in msg
    assert _send(repo)["sent"] == 0  # pending is not approved


def test_slack_failure_leaves_the_draft_for_the_next_run(repo):
    lid, eid = _lead_with_draft(repo)
    lead = asyncio.run(repo.get_lead(lid))
    email = asyncio.run(repo.get_email(eid))

    class Down:
        channel = "#c"

        async def post(self, *a):
            raise RuntimeError("slack 503")

    assert lead and email and lead.contact
    assert not asyncio.run(approval.request(repo, Down(), lead, lead.contact, email))
    assert asyncio.run(repo.get_lead(lid)).status == LeadStatus.EMAIL_DRAFTED
    asyncio.run(repo.open_approval(eid, "#c"))  # a retry does not create a second pending row
    assert (
        repo.db.execute("select count(*) from approvals where ref_id=?", (eid,)).fetchone()[0] == 1
    )


def test_slack_text_is_escaped(repo):
    lead = Lead(company_name="<!channel> & Co", company_website="x.io")
    c = Contact(email="a@x.io")
    e = EmailDraft(email_id="1", lead_id="l", contact_id="c", to="a@x.io", subject="<b>", body="x")
    text = approval.message_text(lead, c, e)
    assert "<!channel>" not in text and "&lt;!channel&gt; &amp; Co" in text
