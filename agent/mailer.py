"""Outgoing mail: the on/off switch and the message (footer, unsubscribe link and headers).

Nothing here sends. The one send path is agent.leadgen.sender, which only accepts emails a person approved.
"""

import hashlib
import hmac
import os
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from agentkit.config import env


def lock_sending() -> None:
    """Make sending impossible for the rest of this process, whatever the settings say. The desktop lead run
    calls this first: it only discovers leads and stores drafts."""
    os.environ["EMAIL_SENDING_LOCKED"] = "1"


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


def allowed_in_env(addr: str) -> bool:
    """In dev, only addresses on ALLOWED_RECIPIENT_DOMAINS may be emailed."""
    if (env("APP_ENV", "dev") or "dev") == "prod":
        return True
    allowed = [
        d.strip().lower()
        for d in (env("ALLOWED_RECIPIENT_DOMAINS", "") or "").split(",")
        if d.strip()
    ]
    return addr.split("@", 1)[1].lower() in allowed
