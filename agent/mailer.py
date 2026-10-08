"""Outgoing mail: the on/off switch and the message (footer, unsubscribe link and headers).

Nothing here sends. The one send path is agent.leadgen.sender, which only accepts emails a person approved.
"""

import hashlib
import hmac
import os
import re
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from html import escape
from pathlib import Path

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


_BULLET = re.compile(r"^\s*(?:[-*\u2022]|\d+[.)])\s+")


def paragraphs(body: str) -> list[str | list[str]]:
    """The draft as paragraphs: blank lines separate them, single line breaks inside one are the model's wrapping and
    are joined. Consecutive "- item" lines become a list (returned as list[str])."""
    out: list[str | list[str]] = []
    for block in re.split(r"\n\s*\n", (body or "").replace("\r\n", "\n").strip()):
        text: list[str] = []
        items: list[str] = []
        for ln in (x.strip() for x in block.split("\n")):
            if not ln:
                continue
            if _BULLET.match(ln):
                if text:
                    out.append(" ".join(text))
                    text = []
                items.append(_BULLET.sub("", ln))
            else:
                if items:
                    out.append(items)
                    items = []
                text.append(ln)
        if text:
            out.append(" ".join(text))
        if items:
            out.append(items)
    return out


def plain_text(body: str) -> str:
    return "\n\n".join(
        "\n".join(f"- {i}" for i in p) if isinstance(p, list) else p for p in paragraphs(body)
    )


def html_body(body: str) -> str:
    style = "margin:0 0 14px 0;"
    parts = []
    for p in paragraphs(body):
        if isinstance(p, list):
            items = "".join(f'<li style="margin:0 0 6px 0;">{escape(i)}</li>' for i in p)
            parts.append(f'<ul style="{style}padding-left:20px;">{items}</ul>')
        else:
            parts.append(f'<p style="{style}">{escape(p)}</p>')
    return "\n".join(parts)


def _template() -> str:
    path = Path(env("EMAIL_TEMPLATE", "config/email_template.html") or "config/email_template.html")
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return (
            "<html><body>{{body}}<p>{{sender_name}}<br>{{company_name}}<br>{{company_address}}</p>"
            '<p><a href="{{unsubscribe_url}}">Unsubscribe</a></p></body></html>'
        )


def _website() -> tuple[str, str]:
    """(shown, link) for COMPANY_WEBSITE; both empty when it is not set."""
    site = (env("COMPANY_WEBSITE", "") or "").strip()
    if not site:
        return "", ""
    return site, site if site.startswith(("http://", "https://")) else f"https://{site}"


def signature_lines() -> list[str]:
    """The sender block, only what is set: person, title, company, website, phone."""
    site, _ = _website()
    values = [
        env("SENDER_NAME", ""),
        env("SENDER_TITLE", ""),
        env("COMPANY_NAME", ""),
        site,
        env("COMPANY_PHONE", ""),
    ]
    return [v.strip() for v in values if v and v.strip()]


def identity_problems() -> list[str]:
    """Settings that would make a mail look unprofessional: placeholders and missing fields. Shown to a person; nothing
    here blocks sending."""
    out = []
    if not (env("SENDER_NAME", "") or "").strip():
        out.append("SENDER_NAME is not set: the mail has no sender name")
    if not (env("COMPANY_WEBSITE", "") or "").strip():
        out.append("COMPANY_WEBSITE is not set: the signature has no website")
    address = (env("COMPANY_ADDRESS", "") or "").strip().lower()
    if address in {"", "address", "your address", "company address", "todo", "tbd"}:
        out.append("COMPANY_ADDRESS is a placeholder: the footer shows it")
    return out


def render_html(subject: str, body: str, unsubscribe: str) -> str:
    """The template filled in. Every value is HTML-escaped: the body comes from a model, the rest from settings."""
    website, url = _website()
    lines = signature_lines()
    signature = "<br>".join(
        f"<strong>{escape(x)}</strong>"
        if i == 0
        else (
            f'<a href="{escape(url)}" style="color:#1f6feb;text-decoration:none;">{escape(x)}</a>'
            if x == website
            else escape(x)
        )
        for i, x in enumerate(lines)
    )
    first = next((p for p in paragraphs(body) if isinstance(p, str)), "")
    values = {
        "subject": escape(subject), "preheader": escape(first[:140]), "body": html_body(body),
        "signature": signature, "sender_title": escape(env("SENDER_TITLE", "") or ""),
        "sender_name": escape(env("SENDER_NAME", "") or ""), "company_name": escape(env("COMPANY_NAME", "") or ""),
        "company_address": escape(env("COMPANY_ADDRESS", "") or ""), "company_website": escape(website),
        "company_website_url": escape(url), "unsubscribe_url": escape(unsubscribe),
    }  # fmt: skip
    return re.sub(r"\{\{(\w+)\}\}", lambda m: values.get(m.group(1), ""), _template())


def build_message(row, to_addr: str) -> EmailMessage:
    """multipart/alternative: a clean plain-text part and the HTML template, with the identity footer and the one-click
    unsubscribe link and headers in both."""
    sender = env("MAIL_FROM", required=True)
    url = unsubscribe_url(str(row["id"]))
    company, address = env("COMPANY_NAME", required=True), env("COMPANY_ADDRESS", required=True)
    sign = "\n".join(signature_lines())
    footer = (
        f"\n\nBest regards,\n{sign}\n\n--\n{company} - {address}\n"
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
    msg.set_content(plain_text(row["body"]) + footer)
    msg.add_alternative(render_html(row["subject"], row["body"], url), subtype="html")
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
