"""The ONE path by which an email leaves the system. Everything that sends (daily run, `agent send`, dry-run) uses
send_approved(); there is no other caller of a transport.

    claim     the database moves approved -> sending only for emails that have a recorded human approval and, for an
              intro, whose lead is APPROVED (repository.claim_sendable: the gate is part of the query)
    re-check  sendable_problem() is asked again right before sending (defence in depth)
    clear     only then is a Cleared object made; a transport refuses anything that is not one
    send      Resend (real) or the outbox folder (dry-run, nothing leaves the machine)

Unapproved, rejected, unknown, suppressed or (in dev) off-allowlist addresses are never sent.
"""

import asyncio
from dataclasses import dataclass, field
from email.utils import make_msgid
from pathlib import Path
from typing import Protocol

from agentkit import resend
from agentkit.config import env
from agentkit.log import get_logger

from .. import mailer
from .models import EmailDraft
from .repository import Repository

log = get_logger("agent.leadgen.sender")
MAX_ATTEMPTS = 3
_GATE = object()  # only this module can make a Cleared


class NotApprovedError(RuntimeError):
    """An attempt to send an email that has not passed the approval gate."""


@dataclass(frozen=True)
class Cleared:
    email: EmailDraft
    to: str
    subject: str
    token: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.token is not _GATE:
            raise NotApprovedError(
                "a Cleared email can only be made by the sender after the approval gate"
            )


class Transport(Protocol):
    name: str

    async def send(self, item: Cleared) -> tuple[str, str]:
        """(Message-ID, provider id)."""
        ...


def _check(item: object) -> Cleared:
    if not isinstance(item, Cleared) or item.token is not _GATE:
        raise NotApprovedError("transport refused: not cleared by the approval gate")
    return item


class ResendTransport:
    name = "resend"

    async def send(self, item: Cleared) -> tuple[str, str]:
        c = _check(item)
        if not mailer.sending_enabled():
            raise NotApprovedError(
                "email sending is disabled (EMAIL_SENDING_ENABLED is not true, or locked)"
            )
        row = {
            "id": c.email.email_id,
            "subject": c.subject,
            "body": c.email.body,
            "in_reply_to": c.email.in_reply_to,
        }
        first: tuple[str, str] | None = None
        errors: list[str] = []
        for to in (
            a.strip() for a in c.to.split(",") if a.strip()
        ):  # Resend rejects a request if one address fails
            msg = mailer.build_message(row, to)
            try:
                pid = await resend.send_email(
                    mailer.resend_payload(msg, to), f"{c.email.email_id}:{to}"
                )
            except Exception as exc:
                errors.append(f"{type(exc).__name__}: {str(exc)[:120]}")
                continue
            first = first or (str(msg["Message-ID"]), pid)
        if first is None:
            raise RuntimeError("; ".join(errors) or "no recipient")
        return first


class OutboxTransport:
    """Dry-run: the message is written to <out>/outbox/<email id>.txt. Nothing is sent."""

    name = "outbox"

    def __init__(self, out_dir: Path) -> None:
        self.dir = out_dir / "outbox"

    async def send(self, item: Cleared) -> tuple[str, str]:
        c = _check(item)
        self.dir.mkdir(parents=True, exist_ok=True)
        msg_id = make_msgid(domain="dry-run.invalid")
        (self.dir / f"{c.email.email_id}.txt").write_text(
            f"DRY RUN - NOT SENT\nTo: {c.to}\nSubject: {c.subject}\nMessage-ID: {msg_id}\n\n{c.email.body}\n",
            encoding="utf-8",
        )
        return msg_id, "dry-run"


def make_transport(dry_run: bool, out_dir: Path) -> Transport | None:
    """None = sending is off (the default): approved emails simply wait."""
    if dry_run:
        return OutboxTransport(out_dir)
    if mailer.sending_enabled():
        return ResendTransport()
    return None


async def send_approved(
    repo: Repository,
    transport: Transport | None,
    *,
    limit: int | None = None,
    gap_seconds: float | None = None,
) -> dict:
    stats = {"sent": 0, "skipped": 0, "failed": 0, "blocked": 0}
    if transport is None:
        log.info("Email sending is disabled (EMAIL_SENDING_ENABLED is not true); nothing sent")
        return {**stats, "disabled": 1}
    cap = int(env("DAILY_SEND_CAP_PER_MAILBOX", "10") or 10)
    gap = float(env("SEND_GAP_SECONDS", "20") or 20) if gap_seconds is None else gap_seconds
    mailbox = env("MAIL_FROM", "dry-run@example.invalid") or "dry-run@example.invalid"
    claimed = await repo.claim_sendable(limit or cap)
    for n, email in enumerate(claimed):
        if problem := await repo.sendable_problem(email.email_id):
            stats["blocked"] += 1
            await repo.mark_send_failed(email.email_id, f"gate: {problem}", final=True)
            log.error(
                "Send blocked by the approval gate",
                extra={"ctx": {"email_id": email.email_id, "why": problem}},
            )
            continue
        override = mailer.test_recipient()
        if await repo.is_suppressed(email.to) or (
            not override and not _allowed(email.to, transport)
        ):
            await repo.mark_send_failed(
                email.email_id, "suppressed or not allowed in this environment", final=True
            )
            stats["skipped"] += 1
            continue
        if not await repo.try_increment_send(mailbox, cap):
            log.info("Daily send cap reached", extra={"ctx": {"cap": cap}})
            for rest in claimed[n:]:
                await repo.release_claim(rest.email_id)
            break
        subject = f"[TEST for {email.to}] {email.subject}" if override else email.subject
        try:
            message_id, provider_id = await transport.send(
                Cleared(email=email, to=override or email.to, subject=subject, token=_GATE)
            )
        except NotApprovedError:
            raise
        except Exception as exc:
            final = email.attempts + 1 >= MAX_ATTEMPTS
            await repo.mark_send_failed(email.email_id, f"{type(exc).__name__}: {exc}", final=final)
            stats["failed"] += 1
            log.exception(
                "Send failed", extra={"ctx": {"email_id": email.email_id, "final": final}}
            )
            continue
        await repo.mark_sent(email.email_id, message_id, provider_id, mailbox)
        stats["sent"] += 1
        log.info("Email sent", extra={"ctx": {"email_id": email.email_id, "transport": transport.name,
                                              "test_redirect": bool(override)}})  # fmt: skip
        if gap and n < len(claimed) - 1:
            await asyncio.sleep(gap)
    log.info("Send done", extra={"ctx": stats})
    return stats


def _allowed(addr: str, transport: Transport) -> bool:
    """The dev recipient allowlist applies to real mail only; the dry-run outbox never sends anything."""
    return transport.name == "outbox" or mailer.allowed_in_env(addr)
