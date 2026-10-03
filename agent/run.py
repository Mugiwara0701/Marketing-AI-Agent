"""One bounded daily run: replies -> send approved emails -> follow-ups -> leads -> blog. Then exit."""

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable

from agentkit import db
from agentkit.log import get_logger

from . import blog, followups, leads, mailer, notify, replies, settings

log = get_logger("agent.run")
SERVICE = "agent"


async def _step(name: str, fn: Callable[[], Awaitable[dict]], seconds: float) -> dict:
    """Run one step as its own agent_runs row; a failure or timeout never stops the next step."""
    run_id = str(uuid.uuid4())
    await db.start_run(SERVICE, name, run_id)
    try:
        async with asyncio.timeout(seconds):
            result = await fn()
        await db.finish_run(run_id, "succeeded", result)
    except Exception as exc:
        log.exception("step failed", extra={"ctx": {"step": name}})
        await db.finish_run(run_id, "failed", None, f"{type(exc).__name__}: {exc}"[:500])
        return {"error": type(exc).__name__}
    return result


async def daily_run(
    *, force: bool = False, only: str | None = None, redo_blog: bool = False
) -> dict:
    cfg = settings.load()
    if not force and await db.fetchrow(
        "select 1 from agent_runs where service=$1 and job='daily_run' and started_at::date = current_date "
        "and (status='succeeded' or (status='running' and started_at > now() - interval '3 hours'))",
        SERVICE,
    ):
        log.info("daily run already done or in progress; use --force to override")
        return {"skipped": True}
    if redo_blog:
        removed = await db.execute(
            "delete from content_posts where created_at::date = current_date"
        )  # variants cascade
        log.info("redo-blog: removed today's post", extra={"ctx": {"result": removed}})
    start = time.monotonic()
    total = cfg.max_minutes * 60
    run_id = str(uuid.uuid4())
    await db.start_run(SERVICE, "daily_run", run_id)
    out: dict = {}
    try:
        # Replies first: it queues answers a person approved, so the send step below delivers them.
        if only in (None, "replies"):
            out["replies"] = await _step("replies", replies.run, 20 * 60)
        if only == "send" or (only is None and mailer.sending_enabled()):
            out["send"] = await _step("send", mailer.send_approved, 15 * 60)
        if only in (None, "followups"):
            deadline = time.monotonic() + 10 * 60
            out["followups"] = await _step("followups", lambda: followups.run(deadline), 12 * 60)
        if only in (None, "leads"):
            budget = total - cfg.blog_reserve_minutes * 60 - (time.monotonic() - start)
            deadline = time.monotonic() + budget
            out["leads"] = await _step("leads", lambda: leads.run(deadline), max(budget, 60) + 120)
        if only in (None, "blog"):
            out["blog"] = await _step(
                "blog", blog.run, max(total - (time.monotonic() - start), 300)
            )
        failed = any("error" in v for v in out.values())
        await db.finish_run(run_id, "failed" if failed else "succeeded", {"processed": len(out)})
    except BaseException as exc:
        await db.finish_run(run_id, "failed", None, type(exc).__name__)
        raise
    try:
        await notify.summary(out)
    except Exception:
        log.warning("slack summary not sent")
    return out
