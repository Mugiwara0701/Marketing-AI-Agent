"""The single long-running service (`agent start`): loops side by side, failures isolated, clean stop."""

import asyncio
from datetime import datetime

from agent import supervisor


def test_next_daily_run_time():
    assert supervisor.next_daily_at(datetime(2026, 10, 6, 8, 0), "09:30") == datetime(
        2026, 10, 6, 9, 30
    )
    assert supervisor.next_daily_at(datetime(2026, 10, 6, 9, 30), "09:30") == datetime(
        2026, 10, 7, 9, 30
    )
    assert supervisor.next_daily_at(datetime(2026, 10, 6, 23, 0), "09:30") == datetime(
        2026, 10, 7, 9, 30
    )


def test_nothing_is_sent_while_sending_is_off(monkeypatch):
    monkeypatch.delenv("EMAIL_SENDING_ENABLED", raising=False)
    called = []

    async def send(**k):
        called.append(1)
        return {}

    from agent.leadgen import service

    monkeypatch.setattr(service, "send", send)
    assert asyncio.run(supervisor.send_once()) is None and called == []


def test_a_failing_loop_does_not_stop_the_others_and_stop_is_clean(monkeypatch):
    calls = {"sender": 0, "inbox": 0, "daily": 0}

    async def sender():
        calls["sender"] += 1

    async def broken_inbox():
        calls["inbox"] += 1
        raise RuntimeError("gmail token expired")

    from agent import run

    async def daily():
        calls["daily"] += 1
        return {"skipped": True}

    monkeypatch.setattr(supervisor, "send_once", sender)
    monkeypatch.setattr(supervisor, "inbox_once", broken_inbox)
    monkeypatch.setattr(run, "daily_run", daily)
    monkeypatch.setenv("SEND_EVERY_SECONDS", "0.01")
    monkeypatch.setenv("INBOX_EVERY_MINUTES", "0.0002")

    async def go():
        task = asyncio.create_task(supervisor.start())
        await asyncio.sleep(0.3)
        import os
        import signal

        os.kill(os.getpid(), signal.SIGTERM)  # what systemctl stop sends
        return await asyncio.wait_for(task, timeout=5)

    assert asyncio.run(go()) == 0
    assert calls["sender"] >= 5  # kept sending while the inbox loop failed
    assert calls["inbox"] >= 2  # retried, with backoff
    assert calls["daily"] == 1  # today's run once at start, the next one tomorrow
