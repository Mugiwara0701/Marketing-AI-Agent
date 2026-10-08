"""Human approval of drafted emails.

    request   EMAIL_DRAFTED -> a pending approval row -> Slack message (Approve / Reject) -> PENDING_APPROVAL
    decide    a person clicks in Slack (supabase/functions/slack-interact -> SQL decide_email()) or uses the CLI
              (python -m agent leads approve|reject <email id>): APPROVED or REJECTED, recorded with who and when

Nothing here sends mail. In a dry-run the Slack message is written to a file instead of posted.
"""

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Protocol

from agentkit import slack
from agentkit.config import env
from agentkit.log import get_logger

from .models import Contact, EmailDraft, Lead, LeadStatus
from .repository import Repository

log = get_logger("agent.leadgen.approval")
_MAX = 2900  # Slack section text limit is 3000


def esc(text: str) -> str:
    """Slack mrkdwn treats & < > specially; scraped text must not inject links or mentions."""
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def clip(text: str, n: int) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


def message_text(lead: Lead, contact: Contact, email: EmailDraft) -> str:
    """Everything a person needs to decide, in the order they read it."""
    signals = "\n".join(f"• {t}" for t in lead.technical_requirements[:8]) or "• (none recorded)"
    why = "\n".join(f"• {e.reason}" for e in lead.evidence[:4]) or "• (no evidence recorded)"
    score = lead.score
    penalties = f"  (penalties: {', '.join(score.penalties)})" if score and score.penalties else ""
    who = " · ".join(x for x in (contact.name, contact.role) if x) or "business contact"
    lines = [
        "*New B2B lead found*",
        f"*Company:* {esc(lead.company_name)}  ({esc(lead.company_website)})",
        f"*Industry:* {esc(lead.industry or '-')}    *Product:* {esc(clip(lead.product or '-', 150))}",
        f"*Opportunity:* {esc(clip(lead.opportunity_description or lead.project_description or '-', 500))}",
        f"*Type:* {esc(lead.result_type)} (tier: {esc(lead.customer_tier)})    "
        f"*Signal:* {esc(lead.project_signal.replace('_', ' '))}    *Score:* {lead.lead_score}/100{esc(penalties)}",
        f"*Technical signals:*\n{esc(signals)}",
        f"*Contact:* {esc(who)}",
        f"*Email:* {esc(contact.email)}"
        + (f"    *LinkedIn:* {esc(contact.linkedin)}" if contact.linkedin else ""),
        f"*Why this is a lead:*\n{esc(why)}",
        "*Source:* " + esc(" , ".join(lead.source_urls[:3]) or "-"),
    ]
    if email.review_note:
        lines.append(f":warning: *Checks flagged:* {esc(email.review_note)}")
    return clip("\n".join(lines), _MAX)


def draft_sections(email: EmailDraft) -> list[dict]:
    """The whole email, split at paragraph breaks into Slack sections (3000 characters each): an approver must see every
    word that will be sent, so a long proposal is never cut."""
    chunks: list[str] = []
    cur = f"*Generated email*\n*Subject:* {esc(email.subject)}\n"
    for para in esc(email.body).split("\n\n"):
        if len(cur) + len(para) + 2 > _MAX and cur.strip():
            chunks.append(cur)
            cur = ""
        cur += "\n" + para + "\n"
    chunks.append(cur)
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": clip(c.strip(), _MAX)}}
        for c in chunks
    ]


def blocks(lead: Lead, contact: Contact, email: EmailDraft) -> list[dict]:
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": message_text(lead, contact, email)}},
        *draft_sections(email),
        *slack.approval_blocks(
            "Approve this email for sending?", f"email:{email.email_id}",
            [("Approve", "approve_email"), ("Reject", "skip_email")],
        )[1:],
    ]  # fmt: skip


class Approver(Protocol):
    channel: str

    async def post(self, lead: Lead, contact: Contact, email: EmailDraft) -> str: ...


class SlackApprover:
    def __init__(self) -> None:
        self.channel = env("SLACK_CHANNEL_OUTREACH", "#outreach-approvals") or "#outreach-approvals"

    async def post(self, lead: Lead, contact: Contact, email: EmailDraft) -> str:
        sent = await slack.post_message(
            self.channel, f"New lead: {lead.company_name}", blocks(lead, contact, email)
        )
        return sent["ts"]


class SimulatedApprover:
    """Dry-run: the Slack message goes to a file; approve with `python -m agent leads approve <id> --dry-run`."""

    def __init__(self, out_dir: Path) -> None:
        self.channel = "simulated"
        self.dir = out_dir / "approvals"

    async def post(self, lead: Lead, contact: Contact, email: EmailDraft) -> str:
        self.dir.mkdir(parents=True, exist_ok=True)
        text = message_text(lead, contact, email)
        body = f"{text}\n\n*Subject:* {email.subject}\n\n{email.body}\n\n[Approve] [Reject]  email id: {email.email_id}\n"
        (self.dir / f"{email.email_id}.md").write_text(body, encoding="utf-8")
        return f"simulated:{email.email_id}"


def make_approver(dry_run: bool, out_dir: Path) -> Approver | None:
    if dry_run:
        return SimulatedApprover(out_dir)
    if env("SLACK_BOT_TOKEN"):
        return SlackApprover()
    return None  # no Slack: drafts wait for the CLI (python -m agent leads review)


# A qualified lead with no public contact: company name and website go to a person, who contacts it by hand.
ManualNotifier = Callable[[Lead, str, "str | None"], Awaitable[None]]


def manual_text(lead: Lead, reason: str, form_url: str | None) -> str:
    site = lead.company_website or ""
    url = site if site.startswith("http") else f"https://{site}"
    lines = [
        ":mag: *Manual check: no contact found*",
        f"*Company:* {esc(lead.company_name)}",
        f"*Website:* {esc(url)}",
    ]
    if form_url:
        lines.append(f"*Contact form:* {esc(form_url)}")
    lines.append(f"*Why:* {esc(reason)}")
    if lead.lead_score is not None:
        lines.append(f"*Score:* {lead.lead_score}")
    return clip("\n".join(lines), _MAX)


def _append(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(text + "\n\n")


def make_manual_notifier(dry_run: bool, out_dir: Path) -> ManualNotifier | None:
    """Slack channel SLACK_CHANNEL_MANUAL (default #manual-check). Dry-run: appended to out/.../manual-check.md.
    None without Slack: the leads stay on `python -m agent manual`."""
    if dry_run:

        async def to_file(lead: Lead, reason: str, form_url: str | None) -> None:
            await asyncio.to_thread(
                _append, out_dir / "manual-check.md", manual_text(lead, reason, form_url)
            )

        return to_file
    if not env("SLACK_BOT_TOKEN"):
        return None
    channel = env("SLACK_CHANNEL_MANUAL", "#manual-check") or "#manual-check"

    async def to_slack(lead: Lead, reason: str, form_url: str | None) -> None:
        text = manual_text(lead, reason, form_url)
        await slack.post_message(
            channel,
            f"Manual check: {lead.company_name}",
            [{"type": "section", "text": {"type": "mrkdwn", "text": text}}],
        )

    return to_slack


# A company whose site shows a contact form: name and the page with the form go to a person, who fills it by hand.
FormNotifier = Callable[[Lead, str], Awaitable[None]]


def form_text(lead: Lead, form_url: str) -> str:
    site = lead.company_website or ""
    url = site if site.startswith("http") else f"https://{site}"
    lines = [
        ":memo: *Contact form to fill*",
        f"*Company:* {esc(lead.company_name)}",
        f"*Website:* {esc(url)}",
        f"*Form page:* {esc(form_url)}",
    ]
    if lead.lead_score is not None:
        lines.append(f"*Score:* {lead.lead_score}")
    return clip("\n".join(lines), _MAX)


def make_form_notifier(dry_run: bool, out_dir: Path) -> FormNotifier | None:
    """Slack channel SLACK_CHANNEL_FORM (default #form-fill). Dry-run: appended to out/.../form-fill.md.
    None without Slack: the forms stay on `python -m agent manual`."""
    if dry_run:

        async def to_file(lead: Lead, form_url: str) -> None:
            await asyncio.to_thread(_append, out_dir / "form-fill.md", form_text(lead, form_url))

        return to_file
    if not env("SLACK_BOT_TOKEN"):
        return None
    channel = env("SLACK_CHANNEL_FORM", "#form-fill") or "#form-fill"

    async def to_slack(lead: Lead, form_url: str) -> None:
        await slack.post_message(
            channel,
            f"Contact form: {lead.company_name}",
            [{"type": "section", "text": {"type": "mrkdwn", "text": form_text(lead, form_url)}}],
        )

    return to_slack


async def request(
    repo: Repository, approver: Approver | None, lead: Lead, contact: Contact, email: EmailDraft
) -> bool:
    """Put one draft in front of a person. True when it was posted (the lead is then PENDING_APPROVAL).
    A failed post leaves the lead EMAIL_DRAFTED; the next run posts it again (no duplicate approval row)."""
    if approver is None:
        log.info(
            "Approval waits for the CLI (no Slack configured)",
            extra={"ctx": {"email_id": email.email_id}},
        )
        return False
    await repo.open_approval(email.email_id, approver.channel)
    try:
        message_id = await approver.post(lead, contact, email)
    except Exception as exc:
        log.warning("Slack approval request failed; retried next run",
                    extra={"ctx": {"email_id": email.email_id, "error": str(exc)[:200]}})  # fmt: skip
        return False
    await repo.set_approval_message(email.email_id, approver.channel, message_id)
    assert lead.lead_id  # noqa: S101 - a stored lead always has an id
    await repo.set_status(lead.lead_id, LeadStatus.PENDING_APPROVAL)
    log.info("Slack approval requested",
             extra={"ctx": {"lead_id": lead.lead_id, "email_id": email.email_id, "message": message_id}})  # fmt: skip
    return True


async def decide(
    repo: Repository, email_id: str, approve: bool, by: str, note: str | None = None
) -> str:
    result = await repo.decide_email(email_id, approve, by, note)
    log.info("Approval received", extra={"ctx": {"email_id": email_id, "result": result, "by": by}})
    return result
