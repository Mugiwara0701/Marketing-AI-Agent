import asyncio

from agent import blog, followups, inbox, notify, replies, run
from agent.leadgen import service


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
    monkeypatch.setattr(service, "send", step("send"))
    monkeypatch.setattr(followups, "run", step("followups"))
    monkeypatch.setattr(service, "run_leads", step("leads"))
    monkeypatch.setattr(blog, "run", step("blog"))
    monkeypatch.setattr(notify, "summary", none)


def test_daily_run_order_puts_replies_before_send(monkeypatch):
    order: list[str] = []
    _fake_steps(monkeypatch, order)
    monkeypatch.setenv("EMAIL_SENDING_ENABLED", "true")
    out = asyncio.run(run.daily_run())
    assert (
        order == ["inbox", "replies", "send", "followups", "leads", "blog"] and list(out) == order
    )


def test_only_runs_a_single_step(monkeypatch):
    order: list[str] = []
    _fake_steps(monkeypatch, order)
    asyncio.run(run.daily_run(only="followups"))
    assert order == ["followups"]


def test_dashboard_pass_starts_with_the_lead_search(monkeypatch):
    """The service's loops poll Gmail, answer replies and send; its passes go straight to the lead search."""
    order: list[str] = []
    steps: list[str] = []
    _fake_steps(monkeypatch, order)
    monkeypatch.setenv("EMAIL_SENDING_ENABLED", "true")
    out = asyncio.run(run.daily_run(force=True, leads_first=True, on_step=steps.append))
    assert order == ["leads", "followups", "blog"] and steps == order and list(out) == order
