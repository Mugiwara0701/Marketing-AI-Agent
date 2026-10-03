"""Handle inbound replies: classify, act on the label, draft an answer for a human to approve.

The resend-webhook Edge Function stores each reply as status 'received'. A person decides on the drafted
answer in Slack (classified -> acknowledged = approved, -> handled = skipped). Approved answers become an
emails row in the 'replies' campaign, so the mailer remains the only send path.
"""

import re

from agentkit import db
from agentkit.log import get_logger

from . import notify, store
from .tasks import reply

log = get_logger("agent.replies")
_BATCH = 20
_QUOTE_START = re.compile(r"^(on .{5,120} wrote:|-{2,}\s*original message|from: .+@)", re.IGNORECASE)

_RECEIVED_Q = """select r.id, r.contact_id, r.body, r.subject, r.message_id,
                        e.subject as sent_subject, e.body as sent_body
                   from replies r
                   join contacts c on c.id=r.contact_id
                   left join emails e on e.id=r.email_id
                  where r.status='received' order by r.received_at limit $1"""


def strip_quoted(body: str) -> str:
    """Keep only what the person wrote: drop '> ' lines and everything after 'On ... wrote:'."""
    kept: list[str] = []
    for line in (body or "").splitlines():
        if _QUOTE_START.match(line.strip()):
            break
        if not line.lstrip().startswith(">"):
            kept.append(line)
    return "\n".join(kept).strip()


def build_context(row) -> str:
    sent = f"Subject: {row['sent_subject'] or ''}\n\n{row['sent_body'] or ''}"
    return f"We sent:\n{sent[:2500]}\n\nThey replied:\n{strip_quoted(row['body'])[:4000]}"


async def _apply(row, result: reply.ReplyResult, problems: list[str]) -> str:
    """Store the outcome and act on the label. Returns the new status. Low confidence or failed checks
    never trigger an automatic action: the item goes to a person."""
    note = "; ".join(problems) or None
    draft = result.reply_body.strip() if result.label in reply.NEEDS_REPLY else ""
    status = "classified"
    if not problems:
        if result.label in ("unsubscribe", "bounce"):
            await store.suppress_contact(row["contact_id"], result.label)
            await store.set_company_status(row["contact_id"], "suppressed")
            status = "unsubscribed" if result.label == "unsubscribe" else "handled"
        elif result.label == "not_interested":
            await store.set_company_status(row["contact_id"], "closed")
            status = "handled"
        elif result.label == "ooo":
            status = "handled"
        else:
            await store.set_company_status(row["contact_id"], "engaged")
    await db.execute(
        """update replies set label=$2, draft_response=$3, review_note=$4, status=$5, updated_at=now()
            where id=$1 and status='received'""",
        row["id"], result.label, draft or None, note, status,
    )  # fmt: skip
    return status


async def queue_approved(limit: int = _BATCH) -> int:
    """Approved answers (status 'acknowledged') become approved emails for the mailer, threaded under the reply."""
    queued = 0
    cid = await store.reply_campaign_id()
    rows = await db.fetch(
        """select r.id, r.contact_id, r.message_id, r.draft_response, r.subject,
                  e.subject as sent_subject, e.message_id as sent_message_id
             from replies r left join emails e on e.id=r.email_id
            where r.status='acknowledged' and r.draft_response is not null
            order by r.updated_at limit $1""",
        limit,
    )
    for r in rows:
        try:
            subject = r["subject"] or r["sent_subject"] or "Your message"
            if not subject.lower().startswith("re:"):
                subject = f"Re: {subject}"
            thread = r["message_id"] if (r["message_id"] or "").startswith("<") else r["sent_message_id"]
            await db.execute(
                """insert into emails (contact_id, campaign_id, step, status, subject, body, reply_id,
                                       in_reply_to, idempotency_key)
                   select $1, $2, coalesce(max(step), 0) + 1, 'approved', $3, $4, $5, $6, $7
                     from emails where contact_id=$1 and campaign_id=$2
                   on conflict do nothing""",
                r["contact_id"], cid, subject[:120], r["draft_response"], r["id"], thread, f"reply-{r['id']}",
            )  # fmt: skip
            await db.execute(
                "update replies set status='handled', updated_at=now() where id=$1", r["id"]
            )
            queued += 1
        except Exception:
            log.exception("queueing reply failed", extra={"ctx": {"reply_id": str(r["id"])}})
    return queued


async def run() -> dict:
    stats = {"processed": 0, "drafted": 0, "suppressed": 0, "closed": 0, "review": 0, "failed": 0}
    stats["queued"] = await queue_approved()
    for row in await db.fetch(_RECEIVED_Q, _BATCH):
        try:
            result, problems = await reply.classify_and_draft(build_context(row))
            status = await _apply(row, result, problems)
        except Exception:
            stats["failed"] += 1  # stays 'received' and is retried on the next run
            log.exception("reply failed", extra={"ctx": {"reply_id": str(row["id"])}})
            continue
        stats["processed"] += 1
        if problems:
            stats["review"] += 1
        elif result.label in ("unsubscribe", "bounce"):
            stats["suppressed"] += 1
        elif result.label == "not_interested":
            stats["closed"] += 1
        elif status == "classified":
            stats["drafted"] += 1
        if status == "classified" or (result.label == "not_interested" and not problems):
            await notify.post_reply(row["id"])
    log.info("replies done", extra={"ctx": stats})
    return stats
