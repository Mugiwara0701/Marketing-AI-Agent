"""Storage of the lead pipeline behind one interface.

    PostgresRepository  production: the Supabase schema (companies = leads, contacts, emails, approvals ...)
    SqliteRepository    dry-run and tests: the same model in a local file, nothing shared with production

Status changes are conditional updates ("... where status in (allowed sources)"), so two processes can never
both move a lead, and the sending gate is checked inside the database query that claims an email.
"""

import hashlib
from datetime import datetime
from typing import Protocol

from ..config import Runtime
from ..models import Approval, Contact, EmailDraft, Lead, LeadStatus

# changed: the draft is not what its Slack card showed (a new card is posted; nothing was decided)
DECISIONS = ("approved", "rejected", "already_decided", "expired", "changed", "unknown")

# What a claim gates on, besides the approval: the live campaign's id, NO_LIVE_CAMPAIGN when the Campaign Agent says
# nothing is live, or None when the link is not configured (then campaigns are not checked at all).
NO_LIVE_CAMPAIGN = "none"


def content_sha256(to: str | None, subject: str | None, body: str | None) -> str:
    """What an approval covers: recipient, subject and body. Same as the SQL function email_content_sha256()."""
    text = f"{(to or '').strip(' ').lower()}\n{subject or ''}\n{body or ''}"
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class Repository(Protocol):
    name: str

    # pages and queries (what was already looked at: a restart does not redo it)
    async def page_seen(self, key: str) -> bool: ...
    async def record_page(
        self, key: str, url: str, kind: str, extracted: dict, lead_id: str | None = None
    ) -> None: ...
    async def query_last_run(self, query: str) -> datetime | None: ...
    async def record_query(
        self, query: str, family: str, engine: str, results: int, opened: int
    ) -> None: ...

    # leads
    async def find_lead(self, *, domain: str = "", name_key: str = "") -> Lead | None: ...
    async def get_lead(self, lead_id: str) -> Lead | None: ...
    async def insert_lead(
        self, lead: Lead, *, source: str, campaign: tuple[str, int] | None = None
    ) -> str: ...
    async def save_lead(self, lead: Lead) -> None: ...
    async def set_status(self, lead_id: str, new: LeadStatus, *, note: str = "") -> bool: ...
    async def leads_with_status(
        self, statuses: list[LeadStatus], limit: int = 50
    ) -> list[Lead]: ...
    async def drafted_since(self, since: datetime) -> int: ...
    async def set_manual(self, lead_id: str, form_url: str | None, reason: str | None) -> bool:
        """Store the contact form / reason; True when this form URL was not stored before."""
        ...

    async def record_contact_attempt(self, lead_id: str) -> int: ...
    async def leads_needing_contact(self, max_attempts: int, limit: int) -> list[Lead]: ...

    # contacts and emails
    async def save_contact(self, lead_id: str, contact: Contact) -> str: ...
    async def contacts_for(self, lead_id: str) -> list[Contact]: ...
    async def save_email_draft(
        self, lead_id: str, contact_id: str, subject: str, body: str, note: str | None
    ) -> str | None: ...
    async def email_for_lead(self, lead_id: str) -> EmailDraft | None: ...
    async def get_email(self, email_id: str) -> EmailDraft | None: ...
    async def drafts(self, limit: int = 100) -> list[EmailDraft]: ...

    # approval
    async def open_approval(
        self, email_id: str, channel: str, shown_sha256: str | None = None
    ) -> None: ...
    async def set_approval_message(self, email_id: str, channel: str, message_id: str) -> None: ...
    async def get_approval(self, email_id: str) -> Approval | None: ...
    async def decide_email(
        self, email_id: str, approve: bool, by: str, note: str | None = None
    ) -> str: ...

    # sending (the gate)
    async def claim_sendable(
        self, limit: int, *, live_campaign: str | None = None
    ) -> list[EmailDraft]: ...
    async def stale_sending(self, minutes: int) -> int:
        """How many emails have been 'sending' for longer than `minutes` (a crash between Gmail and the database)."""
        ...

    async def stuck_sending(self, minutes: int, limit: int) -> list[EmailDraft]:
        """Emails 'sending' for longer than `minutes`: an unclear Gmail result or a crash. The sender settles them."""
        ...

    async def mark_send_unknown(self, email_id: str, error: str) -> None:
        """Gmail may have accepted it: the email stays 'sending' (never released), with the reason recorded."""
        ...

    async def expire_approvals(self) -> int: ...
    async def sendable_problem(self, email_id: str) -> str | None: ...
    async def release_claim(self, email_id: str) -> None: ...
    async def mark_sent(
        self,
        email_id: str,
        message_id: str,
        provider_id: str,
        mailbox: str,
        thread_id: str | None = None,
        *,
        test_redirect: bool = False,
    ) -> None: ...
    async def thread_id_for(self, email_id: str) -> str | None: ...
    async def mark_send_failed(self, email_id: str, error: str, *, final: bool) -> None: ...
    async def is_suppressed(self, addr: str) -> bool: ...
    async def try_increment_send(self, mailbox: str, cap: int) -> bool: ...

    async def close(self) -> None: ...


async def open_repository(rt: Runtime) -> Repository:
    """Postgres for real runs (DATABASE_URL); SQLite for dry-runs, or when no database is configured."""
    if rt.uses_postgres:
        from .postgres import PostgresRepository  # noqa: PLC0415 - asyncpg only when needed

        return PostgresRepository()
    from .sqlite import SqliteRepository  # noqa: PLC0415

    return SqliteRepository(rt.sqlite_path)
