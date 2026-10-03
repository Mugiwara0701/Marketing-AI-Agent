"""Send APPROVED emails through the Gmail API, with suppression, caps and an unsubscribe link."""

import asyncio
import hashlib
import hmac
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from agentkit import db, gmail
from agentkit.config import env
from agentkit.log import get_logger

from . import settings, store

log = get_logger("agent.mailer")
_MAX_ATTEMPTS = 3


def sending_enabled() -> bool:
    """Off unless EMAIL_SENDING_ENABLED=true. Drafts are still generated and saved while it is off."""
    return (env("EMAIL_SENDING_ENABLED", "false") or "false").lower() == "true"


def test_recipient() -> str | None:
    """TEST_RECIPIENT (comma separated) redirects every outgoing mail to those inboxes; the real contact
    is never emailed. Returns the cleaned, comma-joined list."""
    addrs = [a.strip() for a in (env("TEST_RECIPIENT", "") or "").split(",") if a.strip()]
    return ", ".join(addrs) or None


def unsubscribe_url(email_id: str) -> str:
    """Matches supabase/functions/unsubscribe: token = hex HMAC-SHA256 of the email id."""
    base = env("UNSUBSCRIBE_BASE_URL", required=True)
    token = hmac.new(
        env("UNSUBSCRIBE_SECRET", required=True).encode(), email_id.encode(), hashlib.sha256
    ).hexdigest()
    return f"{base}?e={email_id}&t={token}"


def build_message(row, to_addr: str) -> EmailMessage:
    sender = env("MAIL_FROM", required=True)
    url = unsubscribe_url(str(row["id"]))
    footer = (
        f"\n\n--\n{env('SENDER_NAME', '')}\n{env('COMPANY_NAME', required=True)}\n"
        f"{env('COMPANY_ADDRESS', required=True)}\n"
        f"You received this because your company's public contact details list this address. "
        f"Unsubscribe: {url}"
    )
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = sender, to_addr, row["subject"]
    msg["Date"], msg["Message-ID"] = formatdate(localtime=True), make_msgid()
    msg["List-Unsubscribe"] = f"<{url}>"
    msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    if row.get("in_reply_to"):  # follow-ups and replies thread under the mail they answer
        msg["In-Reply-To"] = msg["References"] = row["in_reply_to"]
    if reply_to := env("REPLY_TO"):
        msg["Reply-To"] = reply_to
    msg.set_content(row["body"] + footer)
    return msg


def _allowed_in_env(addr: str) -> bool:
    """In dev, only addresses on ALLOWED_RECIPIENT_DOMAINS may be emailed."""
    if (env("APP_ENV", "dev") or "dev") == "prod":
        return True
    allowed = [
        d.strip().lower()
        for d in (env("ALLOWED_RECIPIENT_DOMAINS", "") or "").split(",")
        if d.strip()
    ]
    return addr.split("@", 1)[1].lower() in allowed


async def _thread_id_for(row) -> str | None:
    """Gmail thread of the mail this one answers, so follow-ups and replies stay in one conversation."""
    if row.get("reply_id"):
        r = await db.fetchrow("select gmail_thread_id from replies where id=$1", row["reply_id"])
        if r and r["gmail_thread_id"]:
            return r["gmail_thread_id"]
    if row.get("in_reply_to"):
        r = await db.fetchrow(
            "select gmail_thread_id from emails where message_id=$1 and gmail_thread_id is not null limit 1",
            row["in_reply_to"],
        )
        return r["gmail_thread_id"] if r else None
    return None


async def deliver(row, recipients: str, thread_id: str | None = None) -> dict:
    """Send one message per address through Gmail. Returns the first that went out as
    {message_id, gmail_message_id, gmail_thread_id}; raises only if every address failed.
    A Gmail auth problem is raised at once: retrying other addresses cannot fix it."""
    first: dict | None = None
    errors: list[str] = []
    service = gmail.get_service()
    for to in (a.strip() for a in recipients.split(",") if a.strip()):
        msg = build_message(row, to)
        try:
            sent = await asyncio.to_thread(gmail.send_message, service, msg, thread_id)
        except gmail.GmailAuthError:
            raise
        except Exception as exc:  # error text only: Gmail errors carry no bodies, never log addresses
            errors.append(f"{type(exc).__name__}: {exc}")
            log.warning("send to one recipient failed", extra={"ctx": {"error": type(exc).__name__}})
            continue
        first = first or sent
    if first is None:
        raise RuntimeError("; ".join(errors) or "no recipient")
    return first


async def send_approved(limit: int | None = None) -> dict:
    stats = {"sent": 0, "skipped": 0, "failed": 0}
    if not sending_enabled():
        log.info("email sending is disabled (EMAIL_SENDING_ENABLED is not true); nothing sent")
        return {**stats, "disabled": 1}
    cfg = settings.load()
    mailbox = env("MAIL_FROM", required=True)
    rows = await db.claim("emails", "approved", "sending", limit or cfg.send_cap)
    for n, row in enumerate(rows):
        contact = await db.fetchrow("select email from contacts where id=$1", row["contact_id"])
        addr = contact["email"] if contact else None
        override = test_recipient()
        try:
            if (
                not addr
                or await store.is_suppressed(addr)
                or (not override and not _allowed_in_env(addr))
            ):
                await db.execute(
                    "update emails set status='skipped', updated_at=now() where id=$1", row["id"]
                )
                stats["skipped"] += 1
                continue
            ok = await db.fetchrow("select try_increment_send($1,$2) as ok", mailbox, cfg.send_cap)
            if not (ok and ok["ok"]):
                await db.execute(
                    "update emails set status='approved', updated_at=now() where id=$1", row["id"]
                )
                log.info("daily send cap reached")
                rest = [r["id"] for r in rows[n + 1 :]]
                if rest:
                    await db.execute(
                        "update emails set status='approved' where id = any($1::uuid[])", rest
                    )
                break
            to_addr = override or addr
            outgoing = row
            if override:  # test mode: show who it would have gone to
                outgoing = {**row, "subject": f"[TEST for {addr}] {row['subject']}"}
            sent = await deliver(outgoing, to_addr, await _thread_id_for(row))
            await db.execute(
                """update emails set status='sent', sent_at=now(), message_id=$2, gmail_message_id=$3,
                          gmail_thread_id=$4, mailbox=$5, updated_at=now() where id=$1""",
                row["id"], sent["message_id"], sent["gmail_message_id"], sent["gmail_thread_id"], mailbox,
            )  # fmt: skip
            stats["sent"] += 1
            await asyncio.sleep(cfg.send_gap_seconds)
        except gmail.GmailAuthError as exc:
            # Not this email's fault: put it and the rest back untouched (no attempt used) and stop.
            stats["failed"] += 1
            log.warning("gmail auth problem, nothing more sent: %s", exc)
            ids = [r["id"] for r in rows[n:]]
            await db.execute("update emails set status='approved' where id = any($1::uuid[])", ids)
            break
        except Exception:
            stats["failed"] += 1
            log.exception("send failed", extra={"ctx": {"email_id": str(row["id"])}})
            back = "expired" if row["attempts"] + 1 >= _MAX_ATTEMPTS else "approved"
            await db.execute(
                "update emails set status=$2, attempts=attempts+1, updated_at=now() where id=$1",
                row["id"],
                back,
            )
    log.info("send done", extra={"ctx": stats})
    return stats
