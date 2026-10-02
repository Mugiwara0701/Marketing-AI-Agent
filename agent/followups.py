"""One follow-up per intro email that was neither opened nor answered after FOLLOWUP_DELAY_DAYS.

The draft goes to Slack for approval like every other email (nothing is sent without a person's
Approve), and is threaded under the intro. A reply of any kind, a bounce, an unsubscribe or a closed
company removes the contact from this list; opens are tracked by the resend-webhook Edge Function.
"""

import time

from agentkit.log import get_logger

from . import notify, settings, store
from .tasks import followup

log = get_logger("agent.followups")


def build_context(row) -> str:
    return (
        f"Company: {row['company']}\n"
        f"Project: {row['project_summary'] or 'not stated'}\n"
        f"Technologies: {', '.join(row['technologies'] or []) or 'not stated'}\n"
        f"Contact name: {row['name'] or 'unknown'}\n"
        f"Contact role: {row['role'] or 'business contact'}\n\n"
        f"First email we sent:\nSubject: {row['subject']}\n\n{(row['body'] or '')[:2000]}"
    )


def reply_subject(subject: str) -> str:
    s = (subject or "").strip()
    return s if s.lower().startswith("re:") else f"Re: {s}"


async def run(deadline: float | None = None) -> dict:
    cfg = settings.load()
    stats = {"processed": 0, "drafted": 0, "failed": 0}
    await store.ensure_followup_step()
    for row in await store.followup_candidates(cfg.followup_delay_days, cfg.followup_max_per_run):
        if deadline is not None and time.monotonic() > deadline:
            break
        try:
            draft, problems = await followup.draft_followup(build_context(row))
            email_id = await store.save_followup_draft(
                row["contact_id"],
                reply_subject(row["subject"])[:120],
                draft.body,
                "; ".join(problems) or None,
                row["message_id"],
            )
            stats["processed"] += 1
            if email_id:
                stats["drafted"] += 1
                await notify.post_email(email_id)
        except Exception:
            stats["failed"] += 1
            log.exception("followup failed", extra={"ctx": {"email_id": str(row["id"])}})
    log.info("followups done", extra={"ctx": stats})
    return stats
