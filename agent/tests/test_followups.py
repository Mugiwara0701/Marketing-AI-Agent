import asyncio

from agent import followups, notify, settings, store
from agent.tasks import followup

BODY = (
    "Following your AOSP bring-up work, a short audit of the HAL layer could be a useful first step. "
    "Would a short call next week be worth it?"
)


def _row(i="e1"):
    return {"id": i, "contact_id": "c1", "subject": "Hello", "body": "Intro text", "message_id": "<m@x>",
            "name": "Ann", "role": "VP", "company": "Acme", "project_summary": "AAOS port",
            "technologies": ["AAOS"]}  # fmt: skip


def test_reply_subject_is_not_doubled():
    assert followups.reply_subject("Hello") == "Re: Hello"
    assert followups.reply_subject("re: Hello") == "re: Hello"


def test_defaults_are_four_days():
    assert settings.load().followup_delay_days == 4


def test_followup_validation():
    def check(body):
        return followup._validate(type("C", (), {"parsed": followup.FollowUp(body=body)})())

    assert check(BODY) == []
    assert check(BODY + " This is risk-free.")
    assert check("Our experience says " + BODY)  # unbacked claim about past work


def _patch(monkeypatch, rows, problems=()):
    saved, posted = [], []

    async def noop():
        return None

    async def candidates(days, limit):
        assert days == 4
        return rows

    async def draft(_ctx):
        return followup.FollowUp(body=BODY), list(problems)

    async def save(contact_id, subject, body, note, in_reply_to):
        saved.append((subject, note, in_reply_to))
        return "new-id"

    async def post(eid):
        posted.append(eid)
        return True

    monkeypatch.setattr(store, "ensure_followup_step", noop)
    monkeypatch.setattr(store, "followup_candidates", candidates)
    monkeypatch.setattr(followup, "draft_followup", draft)
    monkeypatch.setattr(store, "save_followup_draft", save)
    monkeypatch.setattr(notify, "post_email", post)
    return saved, posted


def test_followup_is_drafted_threaded_and_posted_for_approval(monkeypatch):
    saved, posted = _patch(monkeypatch, [_row()])
    out = asyncio.run(followups.run())
    assert saved == [("Re: Hello", None, "<m@x>")] and posted == ["new-id"]
    assert out == {"processed": 1, "drafted": 1, "failed": 0}


def test_failed_checks_are_noted_for_the_reviewer(monkeypatch):
    saved, _ = _patch(monkeypatch, [_row()], problems=["banned phrase: act now"])
    asyncio.run(followups.run())
    assert saved[0][1] == "banned phrase: act now"


def test_one_failure_does_not_stop_the_rest(monkeypatch):
    saved, posted = _patch(monkeypatch, [_row("e1"), _row("e2")])
    calls = {"n": 0}

    async def flaky(_ctx):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("llm down")
        return followup.FollowUp(body=BODY), []

    monkeypatch.setattr(followup, "draft_followup", flaky)
    out = asyncio.run(followups.run())
    assert out["failed"] == 1 and out["drafted"] == 1 and len(posted) == 1


def test_followup_card_is_labelled_and_uses_the_email_buttons():
    row = {"company": "Acme", "domain": "acme.io", "project_summary": "p", "technologies": [],
           "email": "hi@acme.io", "name": "", "role": "", "source_url": "", "review_note": None,
           "subject": "Re: Hello", "body": "Hi", "step": 2}  # fmt: skip
    text, blocks = notify.email_blocks(row, "email:ID-2")
    assert text.startswith("Follow-up") and "Follow-up" in blocks[0]["text"]["text"]
    assert [b["action_id"] for b in blocks[-1]["elements"]] == ["approve_email", "skip_email"]
