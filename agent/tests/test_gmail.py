import asyncio
import base64
import email
import json
from email.message import EmailMessage

import pytest
from google.auth.exceptions import RefreshError

from agent import mailer
from agentkit import gmail


class FakeCreds:
    def __init__(self, *, valid=True, expired=False, refresh_token="rt", fail=False):
        self.valid, self.expired, self.refresh_token, self.fail = valid, expired, refresh_token, fail
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
    assert gmail.load_credentials() is creds and not creds.refreshed


def test_expired_token_is_refreshed_and_saved(tmp_path, monkeypatch):
    creds = FakeCreds(valid=False, expired=True)
    p = _token_file(tmp_path, monkeypatch, creds)
    assert gmail.load_credentials() is creds and creds.refreshed
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
        self.bodies, self.sent, self.stamped, self.fail_get = [], {"id": "g1", "threadId": "t1"} if sent is None else sent, stamped, fail_get

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
    parsed = email.message_from_bytes(base64.urlsafe_b64decode(raw))
    assert parsed["In-Reply-To"] == "<orig@x>" and parsed["References"] == "<orig@x>"
    assert parsed["Message-ID"] == "<ours@x>" and "Hello" in parsed.get_payload(decode=True).decode()


def test_send_parses_the_three_ids_and_sets_thread():
    svc = FakeService()
    out = gmail.send_message(svc, _msg(), "thread-9")
    assert out == {"message_id": "<stamped@mail.gmail.com>", "gmail_message_id": "g1", "gmail_thread_id": "t1"}
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
    assert m["text"] == "plain text" and m["gmail_thread_id"] == "t1" and m["in_reply_to"] == "<m@x>"
    assert gmail.bare_address(m["from"]) == "ann@x.io"


def test_html_only_mail_becomes_safe_text():
    html = "<html><head><style>x{}</style></head><body><p>Hi<br>there</p><script>alert(1)</script></body></html>"
    raw = {"id": "g", "payload": {"mimeType": "text/html", "headers": [], "body": {"data": _b64(html)}}}
    text = gmail.parse_message(raw)["text"]
    assert "Hi" in text and "there" in text and "alert" not in text and "<" not in text


# ---------- mailer on top of Gmail ----------


def _env(monkeypatch):
    for k, v in {"MAIL_FROM": "from@x.io", "COMPANY_NAME": "Co", "COMPANY_ADDRESS": "addr",
                 "UNSUBSCRIBE_BASE_URL": "https://u.io", "UNSUBSCRIBE_SECRET": "s"}.items():  # fmt: skip
        monkeypatch.setenv(k, v)


def _run_send(monkeypatch, row, *, send, thread_rows=None):
    """Drive send_approved with a fake database; returns the recorded UPDATE statements."""
    updates = []

    async def claim(*_a, **_k):
        return [row]

    async def fetchrow(sql, *a):
        if "from contacts" in sql:
            return {"email": "real@company.io"}
        if "try_increment_send" in sql:
            return {"ok": True}
        return (thread_rows or {}).get(a[0])

    async def execute(sql, *a):
        updates.append((sql, a))

    async def not_suppressed(_):
        return False

    async def no_sleep(_):
        return None

    monkeypatch.setattr(mailer.db, "claim", claim)
    monkeypatch.setattr(mailer.db, "fetchrow", fetchrow)
    monkeypatch.setattr(mailer.db, "execute", execute)
    monkeypatch.setattr(mailer.store, "is_suppressed", not_suppressed)
    monkeypatch.setattr(mailer.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(mailer.gmail, "get_service", lambda: object())
    monkeypatch.setattr(mailer.gmail, "send_message", send)
    stats = asyncio.run(mailer.send_approved())
    return stats, updates


def test_gmail_ids_are_persisted_on_send(monkeypatch):
    _env(monkeypatch)
    monkeypatch.setenv("EMAIL_SENDING_ENABLED", "true")
    monkeypatch.setenv("APP_ENV", "prod")
    row = {"id": "e1", "contact_id": "c", "subject": "Hi", "body": "B", "attempts": 0}
    sent = []

    def send(service, msg, thread_id=None):
        sent.append(thread_id)
        return {"message_id": "<g@x>", "gmail_message_id": "gm1", "gmail_thread_id": "th1"}

    stats, updates = _run_send(monkeypatch, row, send=send)
    sql, args = updates[-1]
    assert stats["sent"] == 1 and sent == [None]
    assert "gmail_message_id" in sql and "gmail_thread_id" in sql
    assert args[1:4] == ("<g@x>", "gm1", "th1")


def test_follow_up_is_sent_in_the_intro_thread(monkeypatch):
    _env(monkeypatch)
    monkeypatch.setenv("EMAIL_SENDING_ENABLED", "true")
    monkeypatch.setenv("APP_ENV", "prod")
    row = {"id": "e2", "contact_id": "c", "subject": "Re: Hi", "body": "B", "attempts": 0,
           "in_reply_to": "<intro@x>"}  # fmt: skip
    seen = {}

    def send(service, msg, thread_id=None):
        seen.update(thread=thread_id, irt=msg["In-Reply-To"], refs=msg["References"])
        return {"message_id": "<f@x>", "gmail_message_id": "gm2", "gmail_thread_id": thread_id}

    stats, updates = _run_send(
        monkeypatch, row, send=send, thread_rows={"<intro@x>": {"gmail_thread_id": "th-intro"}}
    )
    assert stats["sent"] == 1
    assert seen == {"thread": "th-intro", "irt": "<intro@x>", "refs": "<intro@x>"}
    assert updates[-1][1][3] == "th-intro"


def test_reply_uses_the_thread_stored_on_the_reply_row(monkeypatch):
    _env(monkeypatch)
    monkeypatch.setenv("EMAIL_SENDING_ENABLED", "true")
    monkeypatch.setenv("APP_ENV", "prod")
    row = {"id": "e3", "contact_id": "c", "subject": "Re: Hi", "body": "B", "attempts": 0,
           "reply_id": "r1", "in_reply_to": "<their@x>"}  # fmt: skip
    seen = []

    def send(service, msg, thread_id=None):
        seen.append(thread_id)
        return {"message_id": "<o@x>", "gmail_message_id": "gm3", "gmail_thread_id": thread_id}

    _run_send(monkeypatch, row, send=send, thread_rows={"r1": {"gmail_thread_id": "th-reply"}})
    assert seen == ["th-reply"]


def test_sending_disabled_sends_nothing(monkeypatch):
    called = []
    monkeypatch.setattr(mailer.gmail, "get_service", lambda: called.append(1))
    assert asyncio.run(mailer.send_approved()) == {"sent": 0, "skipped": 0, "failed": 0, "disabled": 1}
    assert not called


def test_transient_failure_returns_the_email_to_the_queue(monkeypatch):
    _env(monkeypatch)
    monkeypatch.setenv("EMAIL_SENDING_ENABLED", "true")
    monkeypatch.setenv("APP_ENV", "prod")
    row = {"id": "e4", "contact_id": "c", "subject": "Hi", "body": "B", "attempts": 0}

    def send(service, msg, thread_id=None):
        raise OSError("503 backend error")

    stats, updates = _run_send(monkeypatch, row, send=send)
    sql, args = updates[-1]
    assert stats["failed"] == 1 and "attempts=attempts+1" in sql and args[1] == "approved"


def test_last_attempt_expires_the_email(monkeypatch):
    _env(monkeypatch)
    monkeypatch.setenv("EMAIL_SENDING_ENABLED", "true")
    monkeypatch.setenv("APP_ENV", "prod")
    row = {"id": "e5", "contact_id": "c", "subject": "Hi", "body": "B", "attempts": 2}

    def send(service, msg, thread_id=None):
        raise OSError("boom")

    _, updates = _run_send(monkeypatch, row, send=send)
    assert updates[-1][1][1] == "expired"


def test_auth_failure_requeues_without_using_an_attempt(monkeypatch):
    _env(monkeypatch)
    monkeypatch.setenv("EMAIL_SENDING_ENABLED", "true")
    monkeypatch.setenv("APP_ENV", "prod")
    row = {"id": "e6", "contact_id": "c", "subject": "Hi", "body": "B", "attempts": 0}

    def send(service, msg, thread_id=None):
        raise gmail.GmailAuthError("expired")

    stats, updates = _run_send(monkeypatch, row, send=send)
    sql, args = updates[-1]
    assert stats["failed"] == 1 and "attempts" not in sql and args[0] == ["e6"]
