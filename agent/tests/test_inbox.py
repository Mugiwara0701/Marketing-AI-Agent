import asyncio

from agent import inbox, store


def _msg(**kw):
    base = {"gmail_message_id": "g1", "gmail_thread_id": "t1", "labels": ["INBOX"], "message_id": "<r@x>",
            "in_reply_to": "<sent@x>", "references": "<sent@x>", "from": "Ann <ann@c.io>",
            "to": "me@x.io", "subject": "Re: Hi", "date": None, "text": "Sounds good", "html": ""}  # fmt: skip
    return {**base, **kw}


class Fake:
    """In-memory stand-in for the three tables the poller touches."""

    def __init__(self, monkeypatch, msgs, emails=None):
        self.claims: set[str] = set()
        self.events: dict = {}
        self.replies: list = []
        self.updates: list = []
        self.suppressed: list = []
        self.msgs = {m["gmail_message_id"]: m for m in msgs}
        self.emails = emails if emails is not None else [{"id": "e1", "contact_id": "c1", "message_id": "<sent@x>", "gmail_thread_id": "t1"}]
        monkeypatch.setattr(inbox.db, "fetchrow", self.fetchrow)
        monkeypatch.setattr(inbox.db, "execute", self.execute)
        monkeypatch.setattr(inbox.gmail, "get_service", lambda: object())
        monkeypatch.setattr(inbox.gmail, "profile_address", lambda s: "me@x.io")
        monkeypatch.setattr(inbox.gmail, "list_recent_messages", lambda s, q, n: [{"id": i} for i in self.msgs])
        monkeypatch.setattr(inbox.gmail, "get_message", lambda s, i: self.msgs[i])
        monkeypatch.setattr(inbox.store, "suppress_contact", self.suppress)
        monkeypatch.setattr(inbox.store, "set_company_status", self.company)

    async def fetchrow(self, sql, *a):
        if "insert into email_events" in sql:
            if a[0] in self.claims:
                return None
            self.claims.add(a[0])
            return {"id": a[0]}
        if "from emails e" in sql:
            key = a[0]
            for e in self.emails:
                if key == e["gmail_thread_id"] or (isinstance(key, list) and e["message_id"] in key):
                    return e
            return self.emails[0] if self.emails and "join contacts" in sql else None
        return None

    async def execute(self, sql, *a):
        if sql.startswith("insert into replies"):
            if any(r[6] == a[6] for r in self.replies):  # unique gmail_message_id
                return
            self.replies.append(a)
        elif sql.startswith("delete from email_events"):
            self.claims.discard(a[0])
        else:
            self.updates.append((sql, a))

    async def suppress(self, cid, reason):
        self.suppressed.append((cid, reason))

    async def company(self, cid, status):
        pass


def test_reply_is_matched_by_headers_and_stored_once(monkeypatch):
    f = Fake(monkeypatch, [_msg()])
    first = asyncio.run(inbox.poll())
    second = asyncio.run(inbox.poll())  # same message listed again
    assert first["replies"] == 1 and second["duplicate"] == 1 and second["replies"] == 0
    assert len(f.replies) == 1
    r = f.replies[0]
    assert r[0] == "e1" and r[1] == "c1" and r[2] == "<r@x>" and r[3] == "<sent@x>"
    assert r[6] == "g1" and r[7] == "t1"  # gmail_message_id, gmail_thread_id


def test_reply_matched_by_thread_when_headers_are_missing(monkeypatch):
    f = Fake(monkeypatch, [_msg(in_reply_to=None, references=None)])
    assert asyncio.run(inbox.poll())["replies"] == 1 and f.replies[0][0] == "e1"


def test_own_and_unrelated_mail_is_ignored(monkeypatch):
    f = Fake(monkeypatch, [_msg(**{"from": "Me <me@x.io>"}), _msg(gmail_message_id="g2", gmail_thread_id="zz",
                                                                   in_reply_to=None, references=None)], emails=[])
    stats = asyncio.run(inbox.poll())
    assert stats["ignored"] == 2 and not f.replies


def test_failed_message_is_released_for_the_next_poll(monkeypatch):
    f = Fake(monkeypatch, [_msg()])

    def boom(s, i):
        raise OSError("down")

    monkeypatch.setattr(inbox.gmail, "get_message", boom)
    assert asyncio.run(inbox.poll())["failed"] == 1 and not f.claims


def test_mailer_daemon_notice_marks_bounce_and_suppresses(monkeypatch):
    bounce = _msg(**{"from": "Mail Delivery Subsystem <mailer-daemon@googlemail.com>"},
                  subject="Delivery Status Notification (Failure)", in_reply_to=None, references=None,
                  text="550 user unknown\nMessage-ID: <sent@x>")  # fmt: skip
    f = Fake(monkeypatch, [bounce])
    stats = asyncio.run(inbox.poll())
    assert stats["bounces"] == 1 and f.suppressed == [("c1", "bounce")] and not f.replies
    assert any("status='bounced'" in sql for sql, _ in f.updates)


def test_delay_notice_is_not_a_bounce():
    m = _msg(**{"from": "mailer-daemon@googlemail.com"}, subject="Delivery Status Notification (Delay)")
    assert not inbox.is_bounce(m)
    assert inbox.is_bounce(_msg(**{"from": "mailer-daemon@googlemail.com"}, subject="Undelivered Mail"))


def test_followup_query_ignores_opens():
    import inspect

    src = inspect.getsource(store.followup_candidates)
    assert "opened_at" not in src and "bounced_at is null" in src and "from replies" in src
