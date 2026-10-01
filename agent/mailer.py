"""Send APPROVED outreach emails over SMTP, with suppression, caps and an unsubscribe link."""

import asyncio
import hashlib
import hmac
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from agentkit import db
from agentkit.config import env
from agentkit.log import get_logger

from . import settings, store

log = get_logger("agent.mailer")
_MAX_ATTEMPTS = 3


def sending_enabled() -> bool:
    """Off unless EMAIL_SENDING_ENABLED=true. Drafts are still generated and saved while it is off."""
    return (env("EMAIL_SENDING_ENABLED", "false") or "false").lower() == "true"


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
    msg.set_content(row["body"] + footer)
    return msg


def _smtp_send(msg: EmailMessage) -> None:
    host, port = env("SMTP_HOST", required=True), int(env("SMTP_PORT", "587") or 587)
    with smtplib.SMTP(host, port, timeout=60) as s:
        s.starttls(context=ssl.create_default_context())
        user = env("SMTP_USER")
        if user:
            s.login(user, env("SMTP_PASSWORD", required=True))
        s.send_message(msg)


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
        try:
            if not addr or await store.is_suppressed(addr) or not _allowed_in_env(addr):
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
            msg = build_message(row, addr)
            await asyncio.to_thread(_smtp_send, msg)
            await db.execute(
                "update emails set status='sent', sent_at=now(), message_id=$2, mailbox=$3, updated_at=now() where id=$1",
                row["id"], msg["Message-ID"], mailbox,
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
