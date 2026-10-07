"""Start / stop the pipeline from the dashboard, with the office machine behind NAT: nothing ever calls in.

    dashboard backend --HTTPS--> REST API (api/) --> pipeline_control row (Supabase) <--polls every 5 s-- this agent

The dashboard sets the desired state through the API (api/, docs/pipeline-api.md). `link()` reads it every
PIPELINE_POLL_SECONDS (5), starts or stops the pipeline to match, and writes back what it is doing with a heartbeat,
so the dashboard can tell "stopped" from "the office machine is off".

While running, passes (inbox -> replies -> send -> follow-ups -> lead discovery -> blog; agent.run.daily_run) run one
after another with PIPELINE_REST_MINUTES (30) between them; the service's sender and inbox loops only work while
running. Stop cancels the pass in progress. The desired state lives in the database, so a restart (or a reboot)
picks up where the dashboard left it; a fresh install is stopped.
"""

import asyncio
import contextlib
import json
import socket
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from agentkit import db
from agentkit.config import env
from agentkit.log import get_logger

from . import mailer

log = get_logger("agent.control")

STOP_GRACE_SECONDS = 30


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(t: datetime | None) -> str | None:
    return t.isoformat(timespec="seconds") if t else None


class Pipeline:
    """The one switch. `running` is what the sender and inbox loops check; `link()` calls `start`/`stop`."""

    def __init__(self, rest_minutes: float) -> None:
        self.rest_seconds = rest_minutes * 60
        self.running = False
        self.since: datetime | None = None
        self.pass_started: datetime | None = None
        self.step: str | None = None
        self.next_at: datetime | None = None
        self.last: dict | None = None  # {"started_at", "finished_at", "outcome", "result"|"error"}
        self._task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._lock = asyncio.Lock()

    async def start(self) -> bool:
        async with self._lock:
            if self.running:
                return False
            self.running, self.since = True, _now()
            self._wake.clear()
            self._task = asyncio.create_task(self._passes(), name="pipeline")
        log.info("Pipeline started")
        return True

    async def stop(self) -> bool:
        """Cancels the pass in progress and waits (up to STOP_GRACE_SECONDS) for it to end."""
        async with self._lock:
            if not self.running:
                return False
            self.running, self.since, self.next_at = False, _now(), None
            task, self._task = self._task, None
            self._wake.set()
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                    await asyncio.wait_for(task, STOP_GRACE_SECONDS)
        log.info("Pipeline stopped")
        return True

    async def close(self) -> None:
        """Service shutdown: cancel the pass in progress; `running` stays as it was."""
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                await asyncio.wait_for(self._task, STOP_GRACE_SECONDS)

    async def _passes(self) -> None:
        from . import run  # noqa: PLC0415

        while self.running:
            self.next_at, self.step, self.pass_started = None, None, _now()
            outcome: dict[str, Any] = {"started_at": _iso(self.pass_started)}
            try:
                result = await run.daily_run(force=True, on_step=self._on_step)
                outcome |= {
                    "outcome": "failed" if _any_error(result) else "succeeded",
                    "result": result,
                }
            except asyncio.CancelledError:
                self._finish(outcome | {"outcome": "stopped"})
                raise
            except Exception as exc:
                log.exception("Pipeline pass failed")
                outcome |= {"outcome": "failed", "error": f"{type(exc).__name__}: {exc}"[:300]}
            self._finish(outcome)
            log.info("Pipeline pass finished", extra={"ctx": {"outcome": outcome["outcome"]}})
            self.next_at = _now() + timedelta(seconds=self.rest_seconds)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=self.rest_seconds)

    def _on_step(self, name: str) -> None:
        self.step = name

    def _finish(self, outcome: dict) -> None:
        self.pass_started, self.step = None, None
        self.last = outcome | {"finished_at": _iso(_now())}

    def status(self) -> dict:
        return {
            "actual_state": "running" if self.running else "stopped",
            "state_since": self.since,
            "current_step": self.step,
            "current_pass_started_at": self.pass_started,
            "next_pass_at": self.next_at,
            "last_pass": self.last,
            "sending_enabled": mailer.sending_enabled(),
            "test_mode": bool(mailer.test_recipient()),
        }


def _any_error(result: dict) -> bool:
    return any(isinstance(v, dict) and "error" in v for v in result.values())


class ControlStore(Protocol):
    async def load(self) -> dict: ...  # the row: desired_state, last_pass, ...
    async def report(self, status: dict) -> None: ...  # what the agent is doing + heartbeat
    async def offline(self) -> None: ...  # the service is stopping


class PostgresControl:
    """The pipeline_control row (migration 0011)."""

    def __init__(self, host: str | None = None) -> None:
        self.host = host or socket.gethostname()

    async def load(self) -> dict:
        row = await db.fetchrow(
            "select desired_state, last_pass from pipeline_control where id = 1"
        )
        if row is None:
            raise RuntimeError("pipeline_control is missing: run `python -m agent migrate`")
        last = row["last_pass"]
        return {
            "desired_state": row["desired_state"],
            "last_pass": json.loads(last) if last else None,
        }

    async def report(self, status: dict) -> None:
        await db.execute(
            """update pipeline_control
                  set actual_state=$1, state_since=$2, current_step=$3, current_pass_started_at=$4,
                      next_pass_at=$5, last_pass=$6::jsonb, sending_enabled=$7, test_mode=$8, agent_host=$9,
                      heartbeat_at=now(), updated_at=now()
                where id = 1""",
            status["actual_state"], status["state_since"], status["current_step"],
            status["current_pass_started_at"], status["next_pass_at"],
            json.dumps(status["last_pass"], default=str) if status["last_pass"] else None,
            status["sending_enabled"], status["test_mode"], self.host,
        )  # fmt: skip

    async def offline(self) -> None:
        await db.execute(
            """update pipeline_control set actual_state='stopped', current_step=null, current_pass_started_at=null,
                      next_pass_at=null, heartbeat_at=null, updated_at=now() where id = 1"""
        )


async def link(pipeline: Pipeline, store: ControlStore, stop: asyncio.Event, every: float) -> None:
    """Every `every` seconds: apply the desired state, then report. The database being unreachable (a reboot before
    the network is up, an outage) changes nothing: the pipeline keeps its state and the next tick tries again."""
    first, failures = True, 0
    while not stop.is_set():
        try:
            row = await store.load()
            if first:
                pipeline.last = pipeline.last or row.get("last_pass")
                log.info(
                    "Pipeline control linked", extra={"ctx": {"desired": row["desired_state"]}}
                )
                first = False
            if row["desired_state"] == "running":
                await pipeline.start()
            else:
                await pipeline.stop()
            await store.report(pipeline.status())
            if failures:
                log.info("Pipeline control reachable again")
            failures = 0
        except asyncio.CancelledError:
            raise
        except Exception:
            failures += 1
            if (
                failures in (1, 10) or failures % 100 == 0
            ):  # not one line every 5 s during an outage
                log.exception("Pipeline control unreachable", extra={"ctx": {"in_a_row": failures}})
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=every)


async def shutdown(pipeline: Pipeline, store: ControlStore) -> None:
    """Service stopping: cancel the pass, tell the dashboard we are offline. The desired state is left as it is, so
    the next start resumes it."""
    await pipeline.close()
    try:
        await store.offline()
    except Exception:
        log.warning("could not mark the agent offline")


def from_env() -> tuple[Pipeline, float]:
    return (
        Pipeline(float(env("PIPELINE_REST_MINUTES", "30") or 30)),
        float(env("PIPELINE_POLL_SECONDS", "5") or 5),
    )


async def request(state: str, by: str) -> dict:
    """Set the desired state from here (CLI); the dashboard does the same through the REST API."""
    row = await db.fetchrow("select * from request_pipeline($1, $2)", state, by)
    return dict(row) if row else {}


async def read() -> dict:
    row = await db.fetchrow("select * from pipeline_control where id = 1")
    return dict(row) if row else {}
