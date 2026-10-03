"""Poll Gmail: store new replies as status 'received' (agent.replies takes it from there) and mark bounces.

Every Gmail message is claimed once in email_events (provider_event_id = 'gmail:<id>'), so a message is
never fetched or stored twice, whatever the poll overlap. A failure releases the claim for the next poll.
Gmail has no delivery or open webhooks: bounces are found from mailer-daemon notices in the inbox.
"""

import asyncio
import re

from agentkit import db, gmail
from agentkit.log import get_logger

from . import store

log = get_logger("agent.inbox")
_MAX_BODY = 20_000
_QUERY = "in:inbox newer_than:14d"
_BOUNCE_SENDERS = ("mailer-daemon@", "postmaster@")
_MSGID = re.compile(r"<[^<>\s]+@[^<>\s]+>")


def is_bounce(msg: dict) -> bool:
    """A permanent-failure notice from a mail system. 'Delayed' notices are not bounces."""
    sender = gmail.bare_address(msg["from"])
    return sender.startswith(_BOUNCE_SENDERS) and "delay" not in msg["subject"].lower()


def _refs(msg: dict) -> list[str]:
    return _MSGID.findall(f"{msg['in_reply_to'] or ''} {msg['references'] or ''}")


_SENT_COLS = "e.id, e.contact_id, e.message_id, e.gmail_thread_id"  # trusted constant, no user input


async def _match_sent_email(msg: dict, *, by_sender: bool):
    """The mail of ours this message belongs to: Message-ID headers, then Gmail thread, then sender."""
    ids = _refs(msg)
    if ids:
        row = await db.fetchrow(
            f"select {_SENT_COLS} from emails e where e.message_id = any($1::text[]) "  # noqa: S608
            "order by e.sent_at desc nulls last limit 1",
            ids,
        )
        if row:
            return row
    if msg["gmail_thread_id"]:
        row = await db.fetchrow(
            f"select {_SENT_COLS} from emails e where e.gmail_thread_id=$1 and e.status in ('sent','bounced') "  # noqa: S608
            "order by e.sent_at desc nulls last limit 1",
            msg["gmail_thread_id"],
        )
        if row:
            return row
    if by_sender and (addr := gmail.bare_address(msg["from"])):
        return await db.fetchrow(
            f"select {_SENT_COLS} from emails e join contacts c on c.id=e.contact_id "  # noqa: S608
            "where lower(c.email)=$1 and e.status='sent' order by e.sent_at desc limit 1",
            addr,
        )
    return None


async def _store_reply(msg: dict) -> str | None:
    email = await _match_sent_email(msg, by_sender=True)
    if not email:
        return None
    refs = _refs(msg)
    await db.execute(
        """insert into replies (email_id, contact_id, message_id, in_reply_to, subject, body, status,
                                gmail_message_id, gmail_thread_id)
           values ($1,$2,$3,$4,$5,$6,'received',$7,$8) on conflict do nothing""",
        email["id"], email["contact_id"], msg["message_id"] or f"gmail:{msg['gmail_message_id']}",
        refs[0] if refs else email["message_id"], msg["subject"] or None, msg["text"][:_MAX_BODY],
        msg["gmail_message_id"], msg["gmail_thread_id"],
    )  # fmt: skip
    await db.execute("update contacts set last_engaged_at=now() where id=$1", email["contact_id"])
    return str(email["id"])


async def _store_bounce(msg: dict) -> str | None:
    email = await _match_sent_email(msg, by_sender=False)
    if not email:  # also look for our Message-ID inside the quoted original
        quoted = _MSGID.findall(msg["text"])
        if quoted:
            email = await db.fetchrow(
                f"select {_SENT_COLS} from emails e where e.message_id = any($1::text[]) limit 1", quoted  # noqa: S608
            )
    if not email:
        return None
    await db.execute(
        "update emails set status='bounced', bounced_at=now(), updated_at=now() where id=$1 and status='sent'",
        email["id"],
    )
    await store.suppress_contact(email["contact_id"], "bounce")
    await store.set_company_status(email["contact_id"], "suppressed")
    return str(email["id"])


async def _process(service, gmail_id: str, own: str, stats: dict) -> None:
    claimed = await db.fetchrow(
        """insert into email_events (provider_event_id, type, payload) values ($1,'received',$2::jsonb)
           on conflict (provider_event_id) do nothing returning id""",
        f"gmail:{gmail_id}", '{"source": "gmail"}',
    )  # ids only: no addresses or bodies are stored in events
    if not claimed:
        stats["duplicate"] += 1
        return
    try:
        msg = await asyncio.to_thread(gmail.get_message, service, gmail_id)
        kind, email_id = "ignored", None
        if gmail.bare_address(msg["from"]) == own or "SENT" in msg["labels"]:
            pass  # our own mail
        elif is_bounce(msg):
            email_id = await _store_bounce(msg)
            kind = "bounced" if email_id else "ignored"
        else:
            email_id = await _store_reply(msg)
            kind = "received" if email_id else "ignored"
        await db.execute(
            "update email_events set type=$2, email_id=$3 where id=$1", claimed["id"], kind, email_id
        )
        stats[{"received": "replies", "bounced": "bounces"}.get(kind, "ignored")] += 1
    except Exception:
        await db.execute("delete from email_events where id=$1", claimed["id"])  # retried next poll
        stats["failed"] += 1
        log.exception("inbox message failed", extra={"ctx": {"gmail_id": gmail_id}})


async def poll(limit: int = 50) -> dict:
    stats = {"replies": 0, "bounces": 0, "ignored": 0, "duplicate": 0, "failed": 0}
    service = gmail.get_service()
    own = gmail.bare_address(await asyncio.to_thread(gmail.profile_address, service))
    found = await asyncio.to_thread(gmail.list_recent_messages, service, _QUERY, limit)
    for item in reversed(found):  # oldest first
        await _process(service, item["id"], own, stats)
    log.info("inbox done", extra={"ctx": stats})
    return stats
