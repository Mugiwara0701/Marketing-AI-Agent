"""`python -m agent start`: the whole agent as ONE long-running process (systemd: deploy/marketing-agent.service).

Three loops run side by side; one failing never stops the others, and each logs what it did:

    sender   every SEND_EVERY_SECONDS (20): send what a person approved in Slack. Only approved emails (the gate in
             agent/leadgen/sender.py); nothing at all while EMAIL_SENDING_ENABLED is not true.
    inbox    every INBOX_EVERY_MINUTES (10): poll Gmail for replies and bounces, classify replies and draft answers
             (posted to Slack for approval like everything else).
    daily    once a day at DAILY_RUN_AT (09:30, local time), and at start if today's run has not happened yet:
             follow-ups, lead discovery, blog (agent.run.daily_run, with its own time budget).

Approvals happen in Slack whenever a person gets to them; the sender picks them up within seconds. Stop with
`systemctl --user stop marketing-agent` (or Ctrl+C): loops finish their current item and the process exits.
"""

import asyncio
import contextlib
import signal
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta

from agentkit.config import env
from agentkit.log import get_logger

from . import mailer

log = get_logger("agent.supervisor")


def _seconds(name: str, default: float) -> float:
    try:
        return float(env(name, str(default)) or default)
    except ValueError:
        return default


def next_daily_at(now: datetime, at: str) -> datetime:
    """The next local time `at` ("HH:MM") after `now`."""
    hh, mm = (int(x) for x in at.split(":", 1))
    candidate = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    return candidate if candidate > now else candidate + timedelta(days=1)


async def _loop(
    name: str, every: float, job: Callable[[], Awaitable[dict | None]], stop: asyncio.Event
) -> None:
    """Run `job` every `every` seconds until `stop`. Errors are logged with backoff (never a tight crash loop)."""
    failures = 0
    while not stop.is_set():
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


async def daily_loop(at: str, stop: asyncio.Event) -> None:
    """Today's run at start if it has not happened (daily_run skips itself when it has), then every day at `at`."""
    from . import run  # noqa: PLC0415

    while not stop.is_set():
        try:
            log.info("Daily run starting")
            result = await run.daily_run()
            log.info("Daily run finished", extra={"ctx": {"result": result}})
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Daily run failed")
        wake = next_daily_at(datetime.now(), at)
        log.info("Next daily run", extra={"ctx": {"at": wake.isoformat(timespec="minutes")}})
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=(wake - datetime.now()).total_seconds())


async def start() -> int:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    send_every = _seconds("SEND_EVERY_SECONDS", 20)
    inbox_every = _seconds("INBOX_EVERY_MINUTES", 10) * 60
    at = env("DAILY_RUN_AT", "09:30") or "09:30"
    log.info("Agent service started", extra={"ctx": {
        "send_every_s": send_every, "inbox_every_min": inbox_every / 60, "daily_at": at,
        "sending_enabled": mailer.sending_enabled(), "browser": env("BROWSER_BACKEND", "http")}})  # fmt: skip
    started = time.monotonic()
    tasks = [
        asyncio.create_task(_loop("sender", send_every, send_once, stop), name="sender"),
        asyncio.create_task(_loop("inbox", inbox_every, inbox_once, stop), name="inbox"),
        asyncio.create_task(daily_loop(at, stop), name="daily"),
    ]
    await stop.wait()
    log.info(
        "Agent service stopping",
        extra={"ctx": {"uptime_min": round((time.monotonic() - started) / 60)}},
    )
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    return 0
