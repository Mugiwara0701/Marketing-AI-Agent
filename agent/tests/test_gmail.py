import asyncio
import base64
import email
import email.policy
import json
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage

import pytest
from google.auth.exceptions import RefreshError

from agent import mailer
from agent.leadgen import sender
from agent.leadgen.models import Contact, EmailDraft, Lead, LeadStatus
from agent.leadgen.repository.sqlite import SqliteRepository
from agentkit import gmail


class FakeCreds:
    def __init__(self, *, valid=True, expired=False, refresh_token="rt", fail=False):
        self.valid, self.expired, self.refresh_token, self.fail = (
            valid,
            expired,
            refresh_token,
            fail,
        )
        self.refreshed = False

    def refresh(self, _request):
        if self.fail:
            raise RefreshError("invalid_grant")
        self.refreshed, self.valid = True, True

    def to_json(self):
        return json.dumps({"token": "new"})


def _token_file(tmp_path, monkeypatch, creds):
    p = tmp_path / "token.json"
    p.write_text("{}")
    monkeypatch.setenv("GMAIL_TOKEN_PATH", str(p))
    monkeypatch.setenv("GMAIL_CREDENTIALS_PATH", str(tmp_path / "credentials.json"))
    monkeypatch.setattr(gmail.Credentials, "from_authorized_user_file", lambda *_a: creds)
    return p


def test_valid_token_is_reused_without_login(tmp_path, monkeypatch):
    creds = FakeCreds()
    _token_file(tmp_path, monkeypatch, creds)
    got: object = gmail.load_credentials()
    assert got is creds and not creds.refreshed


def test_expired_token_is_refreshed_and_saved(tmp_path, monkeypatch):
    creds = FakeCreds(valid=False, expired=True)
    p = _token_file(tmp_path, monkeypatch, creds)
    got: object = gmail.load_credentials()
    assert got is creds and creds.refreshed
    assert json.loads(p.read_text()) == {"token": "new"}
    assert oct(p.stat().st_mode & 0o777) == "0o600"


def test_refresh_failure_unattended_asks_for_a_new_login(tmp_path, monkeypatch):
    _token_file(tmp_path, monkeypatch, FakeCreds(valid=False, expired=True, fail=True))
    with pytest.raises(gmail.GmailAuthError, match="refresh failed"):
        gmail.load_credentials()


def test_missing_token_unattended_and_missing_credentials_interactive(tmp_path, monkeypatch):
    monkeypatch.setenv("GMAIL_TOKEN_PATH", str(tmp_path / "none.json"))
    monkeypatch.setenv("GMAIL_CREDENTIALS_PATH", str(tmp_path / "credentials.json"))
    with pytest.raises(gmail.GmailAuthError, match="No valid Gmail token"):
        gmail.load_credentials()
    with pytest.raises(gmail.GmailAuthError, match="credentials file not found"):
        gmail.load_credentials(interactive=True)


class FakeService:
    """Records the body of messages().send() and answers get() with the Message-ID Gmail stamped."""

    def __init__(self, sent=None, stamped="<stamped@mail.gmail.com>", fail_get=False):
        self.bodies, self.sent, self.stamped, self.fail_get = (
            [],
            {"id": "g1", "threadId": "t1"} if sent is None else sent,
            stamped,
            fail_get,
        )

    def users(self):
        return self

    def messages(self):
        return self

    def send(self, userId, body):
        self.bodies.append(body)
        return _Exec(self.sent)

    def get(self, **_kw):
        if self.fail_get:
            raise OSError("boom")
        return _Exec({"payload": {"headers": [{"name": "Message-ID", "value": self.stamped}]}})


class _Exec:
    def __init__(self, value):
        self.value = value

    def execute(self, num_retries=0):
        return self.value


def _msg():
    m = EmailMessage()
    m["From"], m["To"], m["Subject"], m["Message-ID"] = "a@b.io", "c@d.io", "Hi", "<ours@x>"
    m["In-Reply-To"] = m["References"] = "<orig@x>"
    m.set_content("Hello ü")
    return m


def test_mime_is_urlsafe_base64_and_round_trips():
    raw = gmail.encode_message(_msg())
    assert "+" not in raw and "/" not in raw
    parsed = email.message_from_bytes(base64.urlsafe_b64decode(raw), policy=email.policy.default)
    assert parsed["In-Reply-To"] == "<orig@x>" and parsed["References"] == "<orig@x>"
    assert parsed["Message-ID"] == "<ours@x>" and "Hello" in parsed.get_content()


def test_send_parses_the_three_ids_and_sets_thread():
    svc = FakeService()
    out = gmail.send_message(svc, _msg(), "thread-9")
    assert out == {
        "message_id": "<stamped@mail.gmail.com>",
        "gmail_message_id": "g1",
        "gmail_thread_id": "t1",
    }
    assert svc.bodies[0]["threadId"] == "thread-9" and "raw" in svc.bodies[0]


def test_first_send_has_no_thread_and_falls_back_to_our_message_id():
    svc = FakeService(fail_get=True)
    out = gmail.send_message(svc, _msg())
    assert "threadId" not in svc.bodies[0] and out["message_id"] == "<ours@x>"


def test_send_without_an_id_is_an_error():
    with pytest.raises(RuntimeError):
        gmail.send_message(FakeService(sent={}), _msg())


def _b64(text):
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def test_parse_message_prefers_plain_text_and_reads_headers():
    raw = {
        "id": "g1", "threadId": "t1", "labelIds": ["INBOX"],
        "payload": {
            "headers": [{"name": "From", "value": "Ann <Ann@X.io>"}, {"name": "Subject", "value": "Re: Hi"},
                        {"name": "Message-ID", "value": "<r@x>"}, {"name": "In-Reply-To", "value": "<m@x>"}],
            "mimeType": "multipart/alternative",
            "parts": [{"mimeType": "text/html", "body": {"data": _b64("<p>html</p>")}},
                      {"mimeType": "text/plain", "body": {"data": _b64("plain text")}}],
        },
    }  # fmt: skip
    m = gmail.parse_message(raw)
    assert (
        m["text"] == "plain text" and m["gmail_thread_id"] == "t1" and m["in_reply_to"] == "<m@x>"
    )
    assert gmail.bare_address(m["from"]) == "ann@x.io"


def test_html_only_mail_becomes_safe_text():
    html = "<html><head><style>x{}</style></head><body><p>Hi<br>there</p><script>alert(1)</script></body></html>"
    raw = {
        "id": "g",
        "payload": {"mimeType": "text/html", "headers": [], "body": {"data": _b64(html)}},
    }
    text = gmail.parse_message(raw)["text"]
    assert "Hi" in text and "there" in text and "alert" not in text and "<" not in text


# ---------- Gmail as the transport of the gated sender ----------


def _env(monkeypatch):
    for k, v in {"MAIL_FROM": "from@x.io", "COMPANY_NAME": "Co", "COMPANY_ADDRESS": "addr",
                 "UNSUBSCRIBE_BASE_URL": "https://u.io", "UNSUBSCRIBE_SECRET": "s",
                 "EMAIL_SENDING_ENABLED": "true", "APP_ENV": "prod"}.items():  # fmt: skip
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("EMAIL_SENDING_LOCKED", raising=False)
    monkeypatch.delenv("TEST_RECIPIENT", raising=False)


class ThreadedRepo(SqliteRepository):
    """The SQLite store with a Gmail conversation to continue (Postgres looks it up from replies / In-Reply-To)."""

    def __init__(self, thread: str | None):
        super().__init__(":memory:")
        self.thread = thread

    async def thread_id_for(self, email_id):
        return self.thread


def _approved(repo, attempts=0):
    async def go():
        lid = await repo.insert_lead(
            Lead(company_name="Co", company_website="company.io"), source="t"
        )
        for st in (LeadStatus.QUALIFIED, LeadStatus.CONTACT_FOUND, LeadStatus.EMAIL_DRAFTED):
            await repo.set_status(lid, st)
        cid = await repo.save_contact(lid, Contact(email="real@company.io", source="u"))
        eid = await repo.save_email_draft(lid, cid, "Hi", "Body", None)
        await repo.decide_email(eid, True, "tester")
        repo.db.execute("update emails set attempts=? where id=?", (attempts, eid))
        return eid

    return asyncio.run(go())


def _send(monkeypatch, repo, send):
    monkeypatch.setattr(gmail, "get_service", lambda interactive=False: object())
    monkeypatch.setattr(gmail, "send_message", send)
    return asyncio.run(sender.send_approved(repo, sender.GmailTransport(), gap_seconds=0))


def test_gmail_ids_are_persisted_on_send(monkeypatch):
    _env(monkeypatch)
    repo = ThreadedRepo(None)
    eid = _approved(repo)
    seen = []

    def send(service, msg, thread_id=None):
        seen.append(thread_id)
        return {"message_id": "<g@x>", "gmail_message_id": "gm1", "gmail_thread_id": "th1"}

    assert _send(monkeypatch, repo, send)["sent"] == 1 and seen == [
        None
    ]  # an intro starts a new thread
    row = repo.db.execute(
        "select status, message_id, provider_id from emails where id=?", (eid,)
    ).fetchone()
    assert tuple(row) == ("sent", "<g@x>", "gm1")


def test_follow_up_and_reply_are_sent_in_their_thread(monkeypatch):
    _env(monkeypatch)
    repo = ThreadedRepo("th-intro")
    _approved(repo)
    seen = []

    def send(service, msg, thread_id=None):
        seen.append(thread_id)
        return {"message_id": "<f@x>", "gmail_message_id": "gm2", "gmail_thread_id": thread_id}

    assert _send(monkeypatch, repo, send)["sent"] == 1 and seen == ["th-intro"]


def test_threading_headers_are_set_on_the_message(monkeypatch):
    _env(monkeypatch)
    draft = EmailDraft(email_id="e", lead_id="l", contact_id="c", to="a@b.io", subject="Re: Hi", body="B",
                       in_reply_to="<intro@x>")  # fmt: skip
    seen: dict = {}

    def send(service, msg, thread_id=None):
        seen.update(thread=thread_id, irt=msg["In-Reply-To"], refs=msg["References"])
        return {"message_id": "<f@x>", "gmail_message_id": "gm2", "gmail_thread_id": thread_id}

    monkeypatch.setattr(gmail, "get_service", lambda interactive=False: object())
    monkeypatch.setattr(gmail, "send_message", send)
    item = sender.Cleared(
        email=draft, to="a@b.io", subject="Re: Hi", token=sender._GATE, thread_id="th-intro"
    )
    sent = asyncio.run(sender.GmailTransport().send(item))
    assert seen == {"thread": "th-intro", "irt": "<intro@x>", "refs": "<intro@x>"}
    assert sent.thread_id == "th-intro"


def test_sending_disabled_sends_nothing(monkeypatch, tmp_path):
    monkeypatch.delenv("EMAIL_SENDING_ENABLED", raising=False)
    called = []
    monkeypatch.setattr(gmail, "get_service", lambda interactive=False: called.append(1))
    assert sender.make_transport(False, tmp_path) is None
    assert asyncio.run(sender.send_approved(SqliteRepository(":memory:"), None))["disabled"] == 1
    assert not called


def _row(repo, eid):
    r = repo.db.execute("select status, attempts, provider_id from emails where id=?", (eid,)).fetchone()
    return tuple(r)


def _age(repo, eid, minutes=20):
    """As if the unclear send happened `minutes` ago (the sender waits before it looks in the Sent folder)."""
    when = (datetime.now(UTC) - timedelta(minutes=minutes)).isoformat(timespec="seconds")
    repo.db.execute("update emails set updated_at=? where id=?", (when, eid))


def _unclear(service, msg, thread_id=None):
    raise OSError("503 backend error")  # Gmail may have accepted it


def test_an_unclear_send_is_never_retried_blindly(monkeypatch):
    _env(monkeypatch)
    repo = ThreadedRepo(None)
    eid = _approved(repo)
    assert _send(monkeypatch, repo, _unclear)["unknown"] == 1
    assert _row(repo, eid) == ("sending", 0, None)  # held, not back in the queue
    calls = []

    def send(service, msg, thread_id=None):
        calls.append(1)
        return {"message_id": "<g@x>", "gmail_message_id": "gm1", "gmail_thread_id": "th1"}

    monkeypatch.setattr(gmail, "find_sent", lambda *a, **k: pytest.fail("looked too early"))
    _send(monkeypatch, repo, send)  # minutes later: still held, and Gmail is not asked yet
    assert calls == [] and _row(repo, eid)[0] == "sending"


def test_an_unclear_send_found_in_gmail_is_recorded_as_sent_not_sent_again(monkeypatch):
    _env(monkeypatch)
    repo = ThreadedRepo(None)
    eid = _approved(repo)
    _send(monkeypatch, repo, _unclear)
    _age(repo, eid)
    asked = []

    def found(service, message_id, to, subject, newer_than_days=3):
        asked.append((message_id, to, subject))
        return {"message_id": message_id, "gmail_message_id": "gm-found", "gmail_thread_id": "th-found"}

    monkeypatch.setattr(gmail, "find_sent", found)
    stats = _send(monkeypatch, repo, lambda *a, **k: pytest.fail("sent twice"))
    assert stats["sent"] == 1 and _row(repo, eid) == ("sent", 0, "gm-found")
    assert asked == [(mailer.message_id_for(eid, "real@company.io"), "real@company.io", "Hi")]


def test_an_unclear_send_not_in_gmail_is_queued_again_with_one_attempt_used(monkeypatch):
    _env(monkeypatch)
    repo = ThreadedRepo(None)
    eid = _approved(repo)
    _send(monkeypatch, repo, _unclear)
    _age(repo, eid)
    monkeypatch.setattr(gmail, "find_sent", lambda *a, **k: None)
    sent = []

    def send(service, msg, thread_id=None):
        sent.append(msg["Message-ID"])
        return {"message_id": msg["Message-ID"], "gmail_message_id": "gm2", "gmail_thread_id": "th2"}

    assert _send(monkeypatch, repo, send)["sent"] == 1  # settled, then sent in the same round
    assert _row(repo, eid) == ("sent", 1, "gm2")
    assert sent == [mailer.message_id_for(eid, "real@company.io")]  # the same Message-ID as the first try


def test_gmail_unreachable_for_the_check_keeps_the_email_held(monkeypatch):
    _env(monkeypatch)
    repo = ThreadedRepo(None)
    eid = _approved(repo)
    _send(monkeypatch, repo, _unclear)
    _age(repo, eid)

    def down(*_a, **_k):
        raise OSError("Gmail unreachable")

    monkeypatch.setattr(gmail, "find_sent", down)
    _send(monkeypatch, repo, lambda *a, **k: pytest.fail("sent while unclear"))
    assert _row(repo, eid)[0] == "sending"  # MA_SEND_STUCK tells a person after 30 minutes


def test_last_attempt_expires_the_email(monkeypatch):
    _env(monkeypatch)
    repo = ThreadedRepo(None)
    eid = _approved(repo, attempts=2)
    _send(monkeypatch, repo, _unclear)
    _age(repo, eid)
    monkeypatch.setattr(gmail, "find_sent", lambda *a, **k: None)
    _send(monkeypatch, repo, lambda *a, **k: pytest.fail("no attempt left"))
    assert _row(repo, eid)[0] == "expired"


def _http_error(status):
    import httplib2
    from googleapiclient.errors import HttpError

    def send(service, msg, thread_id=None):
        raise HttpError(httplib2.Response({"status": str(status)}), b"{}")

    return send


def test_a_refused_send_4xx_is_a_clear_failure_and_queued_again(monkeypatch):
    _env(monkeypatch)
    repo = ThreadedRepo(None)
    eid = _approved(repo)
    assert _send(monkeypatch, repo, _http_error(400))["failed"] == 1
    assert _row(repo, eid)[:2] == ("approved", 1)


def test_a_5xx_is_unclear(monkeypatch):
    _env(monkeypatch)
    repo = ThreadedRepo(None)
    eid = _approved(repo)
    assert _send(monkeypatch, repo, _http_error(503))["unknown"] == 1
    assert _row(repo, eid)[:2] == ("sending", 0)


def test_a_rate_limit_sends_nothing_and_uses_no_attempt(monkeypatch):
    _env(monkeypatch)
    repo = ThreadedRepo(None)
    eid = _approved(repo)
    _send(monkeypatch, repo, _http_error(429))
    assert _row(repo, eid)[:2] == ("approved", 0)


def test_the_send_call_is_never_retried_by_the_client_library():
    seen = []

    class Exec(_Exec):
        def execute(self, num_retries=0):
            seen.append(num_retries)
            return self.value

    svc = FakeService()
    svc.send = lambda userId, body: Exec({"id": "g1", "threadId": "t1"})  # type: ignore[method-assign]
    gmail.send_message(svc, _msg())
    assert seen == [0]


def test_the_message_id_is_fixed_per_email_and_recipient(monkeypatch):
    _env(monkeypatch)
    a = mailer.message_id_for("e1", "Ana@X.io")
    assert a == mailer.message_id_for("e1", " ana@x.io ") and a.endswith("@x.io>")
    assert a != mailer.message_id_for("e1", "bob@x.io") and a != mailer.message_id_for("e2", "ana@x.io")
    msg = mailer.build_message({"id": "e1", "subject": "S", "body": "B"}, "ana@x.io")
    assert msg["Message-ID"] == a


def test_find_sent_looks_by_message_id_then_by_recipient_and_subject(monkeypatch):
    queries = []

    def listing(service, query, max_results=50):
        queries.append(query)
        return [{"id": "g9", "threadId": "t9"}] if "subject:" in query else []

    monkeypatch.setattr(gmail, "list_recent_messages", listing)
    got = gmail.find_sent(object(), "<ma-1@x.io>", "a@b.io", 'Hi "there"')
    assert got == {"message_id": "<ma-1@x.io>", "gmail_message_id": "g9", "gmail_thread_id": "t9"}
    assert queries == ["in:sent rfc822msgid:ma-1@x.io", 'in:sent to:a@b.io subject:"Hi  there" newer_than:3d']


def test_auth_failure_requeues_without_using_an_attempt(monkeypatch):
    _env(monkeypatch)
    repo = ThreadedRepo(None)
    eid = _approved(repo)

    def send(service, msg, thread_id=None):
        raise gmail.GmailAuthError("expired")

    assert _send(monkeypatch, repo, send)["failed"] == 1
    row = repo.db.execute("select status, attempts from emails where id=?", (eid,)).fetchone()
    assert tuple(row) == ("approved", 0)


def test_gmail_transport_refuses_anything_not_cleared(monkeypatch):
    _env(monkeypatch)
    draft = EmailDraft(
        email_id="e", lead_id="l", contact_id="c", to="a@b.io", subject="s", body="b"
    )
    with pytest.raises(sender.NotApprovedError):
        asyncio.run(sender.GmailTransport().send(draft))  # type: ignore[arg-type]
