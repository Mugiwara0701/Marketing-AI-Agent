import asyncio

from agent import blog, followups, inbox, leads, mailer, notify, replies, run


def _fake_steps(monkeypatch, order):
    async def none(*_a, **_k):
        return None

    def step(name, enabled=True):
        async def fn(*_a, **_k):
            order.append(name)
            return {"processed": 1}

        return fn

    monkeypatch.setattr(run.db, "fetchrow", none)
    monkeypatch.setattr(run.db, "start_run", none)
    monkeypatch.setattr(run.db, "finish_run", none)
    monkeypatch.setattr(run.notify, "summary", none)
    monkeypatch.setattr(inbox, "poll", step("inbox"))
    monkeypatch.setattr(replies, "run", step("replies"))
    monkeypatch.setattr(mailer, "send_approved", step("send"))
    monkeypatch.setattr(followups, "run", step("followups"))
    monkeypatch.setattr(leads, "run", step("leads"))
    monkeypatch.setattr(blog, "run", step("blog"))
    monkeypatch.setattr(notify, "summary", none)


def test_daily_run_order_polls_inbox_then_replies_before_send(monkeypatch):
    order: list[str] = []
    _fake_steps(monkeypatch, order)
    monkeypatch.setenv("EMAIL_SENDING_ENABLED", "true")
    out = asyncio.run(run.daily_run())
    assert order == ["inbox", "replies", "send", "followups", "leads", "blog"] and list(out) == order


def test_only_runs_a_single_step(monkeypatch):
    order: list[str] = []
    _fake_steps(monkeypatch, order)
    asyncio.run(run.daily_run(only="followups"))
    assert order == ["followups"]
