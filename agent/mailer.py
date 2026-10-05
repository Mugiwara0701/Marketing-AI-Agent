"""Send APPROVED emails through Resend, with suppression, caps and an unsubscribe link."""

import asyncio
import hashlib
import hmac
import os
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from agentkit import db, resend
from agentkit.config import env
from agentkit.log import get_logger

from . import settings, store

log = get_logger("agent.mailer")
_MAX_ATTEMPTS = 3


def lock_sending() -> None:
    """Make sending impossible for the rest of this process, whatever the settings say. The desktop lead run
    calls this first: it only discovers leads and stores drafts."""
    os.environ["EMAIL_SENDING_LOCKED"] = "1"  # also stops agentkit.resend.send_email


def sending_enabled() -> bool:
    """Off unless EMAIL_SENDING_ENABLED=true (and not locked). Drafts are still generated and saved while off."""
    if os.environ.get("EMAIL_SENDING_LOCKED"):
        return False
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


def resend_payload(msg: EmailMessage, to_addr: str) -> dict:
    """Map the built message to Resend's JSON body; everything but the core fields goes in headers."""
    core = {"from", "to", "subject", "date", "reply-to", "content-type", "mime-version",
            "content-transfer-encoding"}  # fmt: skip
    payload = {
        "from": msg["From"],
        "to": [a.strip() for a in to_addr.split(",")],
        "subject": msg["Subject"],
        "text": msg.get_content(),
        "headers": {k: str(v) for k, v in msg.items() if k.lower() not in core},
    }
    if msg["Reply-To"]:
        payload["reply_to"] = msg["Reply-To"]
    return payload


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


async def deliver(row, recipients: str) -> tuple[str, str]:
    """Send one request per address: Resend rejects the whole request if any single recipient is not
    allowed (e.g. onboarding@resend.dev only reaches the account owner). Returns the first
    (Message-ID, Resend id) that went out; raises only if every address failed."""
    if not sending_enabled():
        raise RuntimeError("email sending is disabled")
    first: tuple[str, str] | None = None
    errors: list[str] = []
    for to in (a.strip() for a in recipients.split(",") if a.strip()):
        msg = build_message(row, to)
        try:
            pid = await resend.send_email(resend_payload(msg, to), f"{row['id']}:{to}")
        except Exception as exc:
            errors.append(f"{to}: {exc}")
            log.warning("send to one recipient failed", extra={"ctx": {"error": str(exc)}})
            continue
        first = first or (msg["Message-ID"], pid)
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
            message_id, provider_id = await deliver(outgoing, to_addr)
            await db.execute(
                "update emails set status='sent', sent_at=now(), message_id=$2, provider_id=$3, mailbox=$4, updated_at=now() where id=$1",
                row["id"], message_id, provider_id, mailbox,
            )  # fmt: skip
            stats["sent"] += 1
            await asyncio.sleep(cfg.send_gap_seconds)
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
