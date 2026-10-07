"""The single long-running service (`agent start`): no schedule, loops side by side, failures isolated, clean stop."""

import asyncio
import os
import signal

from agent import run, supervisor


def test_nothing_is_sent_while_sending_is_off(monkeypatch):
    monkeypatch.delenv("EMAIL_SENDING_ENABLED", raising=False)
    called = []

    async def send(**k):
        called.append(1)
        return {}

    from agent.leadgen import service

    monkeypatch.setattr(service, "send", send)
    assert asyncio.run(supervisor.send_once()) is None and called == []


def _fakes(monkeypatch, calls, desired: str):
    from agent import control
    from agent.tests.test_control import FakeRow

    async def sender():
        calls["sender"] += 1

    async def broken_inbox():
        calls["inbox"] += 1
        raise RuntimeError("gmail token expired")

    async def daily(**_k):
        calls["daily"] += 1
        return {}

    row = FakeRow(desired)
    monkeypatch.setattr(control, "PostgresControl", lambda: row)
    monkeypatch.setattr(supervisor, "send_once", sender)
    monkeypatch.setattr(supervisor, "inbox_once", broken_inbox)
    monkeypatch.setattr(run, "daily_run", daily)
    monkeypatch.setenv("SEND_EVERY_SECONDS", "0.01")
    monkeypatch.setenv("INBOX_EVERY_MINUTES", "0.0002")
    monkeypatch.setenv("PIPELINE_POLL_SECONDS", "0.01")
    monkeypatch.setenv("PIPELINE_REST_MINUTES", "60")
    return row


async def _run_service_briefly() -> int:
    task = asyncio.create_task(supervisor.start())
    await asyncio.sleep(0.4)
    os.kill(os.getpid(), signal.SIGTERM)  # what systemctl stop sends
    return await asyncio.wait_for(task, timeout=10)


def test_running_service_loops_and_a_failing_loop_does_not_stop_the_others(monkeypatch):
    calls = {"sender": 0, "inbox": 0, "daily": 0}
    row = _fakes(monkeypatch, calls, "running")
    assert asyncio.run(_run_service_briefly()) == 0
    assert calls["sender"] >= 5  # kept sending while the inbox loop failed
    assert calls["inbox"] >= 2  # retried, with backoff
    assert calls["daily"] == 1  # one pass, then the rest period
    assert row.reports and row.offline_calls == 1


def test_stopped_pipeline_does_nothing_at_all(monkeypatch):
    calls = {"sender": 0, "inbox": 0, "daily": 0}
    row = _fakes(monkeypatch, calls, "stopped")
    assert asyncio.run(_run_service_briefly()) == 0
    assert calls == {"sender": 0, "inbox": 0, "daily": 0}  # no schedule: waits for the dashboard
    assert row.reports[-1]["actual_state"] == "stopped"  # but keeps reporting a heartbeat
