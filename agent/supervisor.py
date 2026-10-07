"""`python -m agent start`: the whole agent as ONE long-running process (systemd: deploy/marketing-agent.service).

Nothing runs on a schedule: the dashboard starts and stops the pipeline. Nothing calls this machine either: the agent
polls the desired state from the database every PIPELINE_POLL_SECONDS (5) and reports back (agent/control.py).
While it is running:

    passes   one after another, PIPELINE_REST_MINUTES (30) apart: follow-ups, lead discovery, blog
             (agent.run.daily_run, with its own time budget). Stop cancels the pass in progress.
    sender   every SEND_EVERY_SECONDS (20): send what a person approved in Slack. Only approved emails (the gate in
             agent/leadgen/sender.py); nothing at all while EMAIL_SENDING_ENABLED is not true.
    inbox    every INBOX_EVERY_MINUTES (10): poll Gmail for replies and bounces, classify replies and draft answers
             (posted to Slack for approval like everything else).

While stopped the loops idle. One loop failing never stops the others, and each logs what it did. Stop the service
with `systemctl --user stop marketing-agent` (or Ctrl+C); the pipeline's running/stopped state is kept for the restart.
"""

import asyncio
import contextlib
import signal
import time
from collections.abc import Awaitable, Callable

from agentkit.config import env
from agentkit.log import get_logger

from . import control, mailer

log = get_logger("agent.supervisor")


def _seconds(name: str, default: float) -> float:
    try:
        return float(env(name, str(default)) or default)
    except ValueError:
        return default


async def _loop(
    name: str,
    every: float,
    job: Callable[[], Awaitable[dict | None]],
    stop: asyncio.Event,
    active: Callable[[], bool] = lambda: True,
) -> None:
    """Run `job` every `every` seconds until `stop`, skipping turns while not `active()`. Errors are logged with
    backoff (never a tight crash loop)."""
    failures = 0
    while not stop.is_set():
        if not active():
            failures = 0
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=min(every, 5))
            continue
        try:
            result = await job()
            failures = 0
            if result:
                log.info(f"{name} done", extra={"ctx": {"result": result}})
        except asyncio.CancelledError:
            raise
        except Exception:
            failures += 1
            log.exception(f"{name} failed", extra={"ctx": {"in_a_row": failures}})
        wait = every * min(2 ** max(failures - 1, 0), 30) if failures else every
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=wait)


async def send_once() -> dict | None:
    if not mailer.sending_enabled():
        return None
    from .leadgen import service  # noqa: PLC0415

    stats = await service.send()
    return (
        stats
        if stats.get("sent") or stats.get("failed") or stats.get("blocked") or stats.get("skipped")
        else None
    )


async def inbox_once() -> dict | None:
    from . import inbox, replies  # noqa: PLC0415

    polled = await inbox.poll()
    answered = await replies.run()
    busy = any(
        v for k, v in {**polled, **answered}.items() if isinstance(v, int) and k != "processed"
    )
    return {"inbox": polled, "replies": answered} if busy else None


async def start() -> int:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    send_every = _seconds("SEND_EVERY_SECONDS", 20)
    inbox_every = _seconds("INBOX_EVERY_MINUTES", 10) * 60
    pipeline, poll_every = control.from_env()
    store = control.PostgresControl()
    log.info("Agent service started", extra={"ctx": {
        "poll_every_s": poll_every, "send_every_s": send_every, "inbox_every_min": inbox_every / 60,
        "sending_enabled": mailer.sending_enabled(), "browser": env("BROWSER_BACKEND", "http")}})  # fmt: skip
    started = time.monotonic()
    is_running = lambda: pipeline.running  # noqa: E731
    tasks = [
        asyncio.create_task(control.link(pipeline, store, stop, poll_every), name="control"),
        asyncio.create_task(
            _loop("sender", send_every, send_once, stop, is_running), name="sender"
        ),
        asyncio.create_task(
            _loop("inbox", inbox_every, inbox_once, stop, is_running), name="inbox"
        ),
    ]
    await stop.wait()
    log.info(
        "Agent service stopping",
        extra={"ctx": {"uptime_min": round((time.monotonic() - started) / 60)}},
    )
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await control.shutdown(pipeline, store)
    return 0
