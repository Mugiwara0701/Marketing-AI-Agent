"""Dashboard start/stop (agent/control.py): the agent polls the desired state, applies it and reports a heartbeat.
Nothing calls the office machine, so these tests drive it through a fake pipeline_control row."""

import asyncio

from agent import control, run


class FakeRow:
    """The pipeline_control row; `fail` makes the database unreachable."""

    def __init__(self, desired="stopped", last_pass=None):
        self.desired = desired
        self.last_pass = last_pass
        self.reports: list[dict] = []
        self.offline_calls = 0
        self.fail = False

    async def load(self):
        if self.fail:
            raise OSError("Temporary failure in name resolution")
        return {"desired_state": self.desired, "last_pass": self.last_pass}

    async def report(self, status):
        self.reports.append(dict(status))

    async def offline(self):
        self.offline_calls += 1


def _slow_pass(monkeypatch, cancelled: list):
    async def slow_run(*, force, on_step):
        assert force  # the once-a-day guard does not apply to a dashboard start
        on_step("leads")
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise
        return {}

    monkeypatch.setattr(run, "daily_run", slow_run)


async def _linked(row, body, every=0.01):
    pipeline, stop = control.Pipeline(rest_minutes=60), asyncio.Event()
    task = asyncio.create_task(control.link(pipeline, row, stop, every))
    try:
        return await body(pipeline)
    finally:
        stop.set()
        await task
        await control.shutdown(pipeline, row)


def test_start_and_stop_follow_the_row_and_stop_cancels_the_pass(monkeypatch):
    cancelled: list = []
    _slow_pass(monkeypatch, cancelled)
    row = FakeRow("stopped")

    async def body(p):
        await asyncio.sleep(0.05)
        idle = p.running
        row.desired = "running"  # the dashboard pressed Start
        await asyncio.sleep(0.05)
        during = dict(row.reports[-1])
        row.desired = "stopped"  # ... and Stop
        await asyncio.sleep(0.05)
        return idle, during, dict(row.reports[-1])

    idle, during, after = asyncio.run(_linked(row, body))
    assert idle is False
    assert during["actual_state"] == "running" and during["current_step"] == "leads"
    assert (
        after["actual_state"] == "stopped" and after["current_step"] is None and cancelled == [True]
    )
    assert after["last_pass"]["outcome"] == "stopped"
    assert row.offline_calls == 1  # service stopping: the dashboard shows offline at once


def test_the_desired_state_survives_a_restart(monkeypatch):
    cancelled: list = []
    _slow_pass(monkeypatch, cancelled)
    row = FakeRow("running", last_pass={"outcome": "succeeded"})

    async def body(p):
        await asyncio.sleep(0.05)
        return p.running, p.status()["last_pass"]

    running, last = asyncio.run(_linked(row, body))
    assert running and last == {"outcome": "succeeded"}  # resumed, and the last pass is still shown
    assert row.desired == "running"  # shutdown does not change what the dashboard asked for


def test_database_outage_keeps_the_pipeline_as_it_is(monkeypatch):
    cancelled: list = []
    _slow_pass(monkeypatch, cancelled)
    row = FakeRow("running")

    async def body(p):
        await asyncio.sleep(0.05)
        row.fail = True  # e.g. network down
        n = len(row.reports)
        await asyncio.sleep(0.05)
        still = p.running and len(row.reports) == n
        row.fail = False
        await asyncio.sleep(0.05)
        return still, len(row.reports) > n

    still_running_no_reports, reports_again = asyncio.run(_linked(row, body))
    assert (
        still_running_no_reports and reports_again and cancelled == [True]
    )  # cancelled only by the shutdown


def test_passes_repeat_after_the_rest_and_errors_are_reported(monkeypatch):
    passes = []

    async def quick_run(**_k):
        passes.append(1)
        return {"leads": {"error": "SearchBlockedError"}}

    monkeypatch.setattr(run, "daily_run", quick_run)

    async def go():
        p = control.Pipeline(rest_minutes=0.001)  # 60 ms
        await p.start()
        await asyncio.sleep(0.3)
        await p.stop()
        return p.status()

    status = asyncio.run(go())
    assert len(passes) >= 2 and status["last_pass"]["outcome"] == "failed"
    assert status["actual_state"] == "stopped" and status["next_pass_at"] is None


def test_start_and_stop_are_idempotent(monkeypatch):
    _slow_pass(monkeypatch, [])

    async def go():
        p = control.Pipeline(rest_minutes=60)
        r = [await p.start(), await p.start(), await p.stop(), await p.stop()]
        return r

    assert asyncio.run(go()) == [True, False, True, False]


def test_a_stopped_step_is_recorded_as_stopped_not_left_running(monkeypatch):
    finished = []

    async def none(*_a, **_k):
        return None

    async def record(run_id, status, result=None, error=None):
        finished.append((status, error))

    async def forever():
        await asyncio.sleep(60)

    monkeypatch.setattr(run.db, "start_run", none)
    monkeypatch.setattr(run.db, "finish_run", record)

    async def go():
        t = asyncio.create_task(run._step("leads", forever, 120))
        await asyncio.sleep(0.01)
        t.cancel()
        await asyncio.gather(t, return_exceptions=True)

    asyncio.run(go())
    assert finished == [("failed", "stopped")]
