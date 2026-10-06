import asyncio
from types import SimpleNamespace

from agent import notify, replies, store
from agent.tasks import reply

GOOD_BODY = (
    "Thanks for getting back to us. We could start with a short bring-up audit of your board and then "
    "agree the next steps. Which two time windows suit you for a short call this week?"
)


def _row(i="r1"):
    return {"id": i, "contact_id": "c1", "body": "Sure, call me", "subject": "Re: Hi",
            "message_id": "<m@x>", "sent_subject": "Hi", "sent_body": "Hello"}  # fmt: skip


def test_strip_quoted_keeps_only_new_text():
    body = "Yes, interested.\n> old quoted line\nThanks\nOn Mon, 5 Jan 2026, Bob <b@x.io> wrote:\n> more"
    assert replies.strip_quoted(body) == "Yes, interested.\nThanks"


def test_reply_validation():
    def check(**kw):
        return reply._validate(SimpleNamespace(parsed=reply.ReplyResult(**kw)))  # type: ignore[arg-type]

    assert check(label="interested", confidence=0.9, reply_body=GOOD_BODY) == []
    assert check(label="unsubscribe", confidence=0.95) == []
    assert check(label="interested", confidence=0.9, reply_body="")  # reply missing
    assert check(label="ooo", confidence=0.3)  # low confidence goes to review
    assert check(label="question", confidence=0.9, reply_body=GOOD_BODY + " Guaranteed results.")


def _patch(monkeypatch, result, problems=()):
    calls: dict[str, list] = {"suppress": [], "company": [], "posted": [], "sql": []}

    async def fetch(sql, *a):
        return [_row()] if "r.status='received'" in sql else []

    async def execute(sql, *a):
        calls["sql"].append(a)

    async def classify(_ctx):
        return result, list(problems)

    async def suppress(cid, reason):
        calls["suppress"].append(reason)

    async def company(cid, status):
        calls["company"].append(status)

    async def post(rid):
        calls["posted"].append(rid)
        return True

    async def campaign():
        return "camp"

    monkeypatch.setattr(replies.db, "fetch", fetch)
    monkeypatch.setattr(replies.db, "execute", execute)
    monkeypatch.setattr(reply, "classify_and_draft", classify)
    monkeypatch.setattr(store, "suppress_contact", suppress)
    monkeypatch.setattr(store, "set_company_status", company)
    monkeypatch.setattr(store, "reply_campaign_id", campaign)
    monkeypatch.setattr(notify, "post_reply", post)
    return calls


def test_unsubscribe_suppresses_without_posting(monkeypatch):
    calls = _patch(monkeypatch, reply.ReplyResult(label="unsubscribe", confidence=0.95))
    out = asyncio.run(replies.run())
    assert calls["suppress"] == ["unsubscribe"] and calls["company"] == ["suppressed"]
    assert calls["posted"] == [] and out["suppressed"] == 1


def test_interested_gets_a_draft_for_approval(monkeypatch):
    res = reply.ReplyResult(label="interested", confidence=0.9, reply_body=GOOD_BODY)
    calls = _patch(monkeypatch, res)
    out = asyncio.run(replies.run())
    assert calls["company"] == ["engaged"] and calls["suppress"] == []
    assert calls["posted"] == ["r1"] and out["drafted"] == 1


def test_failed_checks_never_trigger_automatic_actions(monkeypatch):
    res = reply.ReplyResult(label="unsubscribe", confidence=0.3)
    calls = _patch(monkeypatch, res, problems=["confidence 0.30 < 0.6"])
    out = asyncio.run(replies.run())
    assert calls["suppress"] == [] and calls["company"] == []
    assert calls["posted"] == ["r1"] and out["review"] == 1


def test_classifier_error_leaves_reply_for_retry(monkeypatch):
    calls = _patch(monkeypatch, None)

    async def boom(_ctx):
        raise RuntimeError("llm down")

    monkeypatch.setattr(reply, "classify_and_draft", boom)
    out = asyncio.run(replies.run())
    assert out["failed"] == 1 and out["processed"] == 0 and calls["posted"] == []
