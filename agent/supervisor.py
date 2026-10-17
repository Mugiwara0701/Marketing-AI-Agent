"""`python -m agent start`: the whole agent as ONE long-running process (systemd: deploy/marketing-agent.service).

Nothing runs on a schedule: the dashboard starts and stops the pipeline. Nothing calls this machine either: the agent
polls the desired state from the database every PIPELINE_POLL_SECONDS (2) and reports back (agent/control.py).
While it is running:

    passes   one after another, PIPELINE_REST_MINUTES (30) apart: lead discovery first (Chrome starts within
             seconds of Start), then follow-ups, blog (agent.run.daily_run, own time budget). Stop cancels the pass.
    sender   every SEND_EVERY_SECONDS (20): send what a person approved in Slack. Only approved emails (the gate in
             agent/leadgen/sender.py); nothing at all while EMAIL_SENDING_ENABLED is not true.
    manual   every MANUAL_EVERY_SECONDS (30): a contact a person added on the dashboard for a lead the agent found no
             contact for: draft the email and post it to Slack for approval (agent.leadgen.service.manual_contacts).
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

from . import campaign_link, control, mailer

log = get_logger("agent.supervisor")


def _seconds(name: str, default: float) -> float:
    try:
        return float(env(name, str(default)) or default)
    except ValueError:
        return default


# What a repeated failure of a background job means to the Campaign Agent: (code, dedupe key, message). Fixed texts only.
_BLOCKERS = {
    "inbox": ("MA_INBOX_FAILED", "inbox", "The inbox could not be read"),
    "sender": ("MA_SEND_FAILED", "send", "Approved emails could not be sent"),
}
_GMAIL_AUTH = (
    "MA_GMAIL_AUTH",
    "gmail-auth",
    "The Gmail login expired: someone must log in again on the Marketing Agent machine",
)


def _report_failure(name: str, exc: BaseException) -> None:
    code, key, message = (
        _GMAIL_AUTH
        if type(exc).__name__ == "GmailAuthError"
        else _BLOCKERS.get(name, (None, None, None))
    )
    if code and message:
        campaign_link.emit("blocker", "error", code, message, dedupe_key=key)


def _report_recovered(name: str) -> None:
    for code, key, _ in ([_GMAIL_AUTH] if name == "inbox" else []) + (
        [_BLOCKERS[name]] if name in _BLOCKERS else []
    ):
        campaign_link.emit("resolved", "info", code, "Working again", dedupe_key=key)


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
    reported = (
        False  # a restart forgets earlier failures: the first good run says "working again" once
    )
    while not stop.is_set():
        if not active():
            failures = 0
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=min(every, 5))
            continue
        try:
            result = await job()
            if failures or not reported:
                _report_recovered(name)
            failures, reported = 0, True
            if result:
                log.info(f"{name} done", extra={"ctx": {"result": result}})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failures += 1
            log.exception(f"{name} failed", extra={"ctx": {"in_a_row": failures}})
            _report_failure(name, exc)
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


async def manual_once() -> dict | None:
    from .leadgen import service  # noqa: PLC0415

    return await service.manual_contacts() or None


async def inbox_once() -> dict | None:
    from . import inbox, replies  # noqa: PLC0415

    polled = await inbox.poll()
    answered = await replies.run()
    busy = any(
        v for k, v in {**polled, **answered}.items() if isinstance(v, int) and k != "processed"
    )
    return {"inbox": polled, "replies": answered} if busy else None


async def housekeeping_once() -> dict | None:
    """Hourly, whether or not the pipeline runs: expire approval requests past their date (the lead is posted again),
    and the 12-month contact retention. Deleting contacts is permanent, so it only runs with RETENTION_PURGE_ENABLED=true;
    otherwise the number that would go is reported."""
    from agentkit import db  # noqa: PLC0415

    expired = await db.fetchrow("select expire_approvals() as n")
    out: dict = {"approvals_expired": int(expired["n"]) if expired else 0}
    if (env("RETENTION_PURGE_ENABLED", "false") or "false").lower() == "true":
        purged = await db.fetchrow("select purge_stale_contacts() as n")
        out["contacts_purged"] = int(purged["n"]) if purged else 0
    else:
        due = await db.fetchrow(
            """select count(*) as n from contacts
                where coalesce(last_engaged_at, collected_at) < now() - interval '12 months'"""
        )
        out["contacts_due_for_purge"] = int(due["n"]) if due else 0
    return out if any(out.values()) else None


async def start() -> int:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    send_every = _seconds("SEND_EVERY_SECONDS", 20)
    inbox_every = _seconds("INBOX_EVERY_MINUTES", 10) * 60
    manual_every = _seconds("MANUAL_EVERY_SECONDS", 30)
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
            _loop("manual", manual_every, manual_once, stop, is_running), name="manual"
        ),
        asyncio.create_task(
            _loop("inbox", inbox_every, inbox_once, stop, is_running), name="inbox"
        ),
        asyncio.create_task(
            _loop("housekeeping", _seconds("HOUSEKEEPING_EVERY_SECONDS", 3600), housekeeping_once, stop),
            name="housekeeping",
        ),
    ]
    if (
        campaign_link.link_config() is not None
    ):  # the direct link to the Campaign Agent (off unless configured)
        tasks.append(
            asyncio.create_task(campaign_link.run_sender(stop), name="campaign-link-events")
        )
        from .leadgen import config as leadgen_config  # noqa: PLC0415

        tasks.append(  # picks a new live campaign up in seconds, even while the pipeline is stopped
            asyncio.create_task(
                campaign_link.run_watcher(stop, lambda: leadgen_config.load().raw),
                name="campaign-link-watch",
            )
        )
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
