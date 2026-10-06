"""Postgres (Supabase) repository: companies are leads; contacts, emails and approvals as in the core schema.

Needs migration 0007_lead_pipeline.sql (`python -m agent migrate`)."""

import json
from datetime import datetime
from uuid import UUID

from agentkit import db

from ..models import (
    Approval,
    Contact,
    EmailDraft,
    Evidence,
    Lead,
    LeadStatus,
    ScoreCard,
    sources_of,
)
from .sqlite import _problem

# Legacy companies.status kept in step for the dashboard, replies and follow-ups.
_LEGACY = {
    LeadStatus.DISCOVERED: "candidate", LeadStatus.QUALIFIED: "qualified", LeadStatus.CONTACT_FOUND: "contact_found",
    LeadStatus.EMAIL_DRAFTED: "contact_found", LeadStatus.PENDING_APPROVAL: "contact_found",
    LeadStatus.APPROVED: "contact_found", LeadStatus.REJECTED: "rejected", LeadStatus.SENT: "contact_found",
    LeadStatus.FAILED: "contact_found",
}  # fmt: skip


def _json(v) -> list | dict:
    if v is None:
        return []
    return json.loads(v) if isinstance(v, str) else v


class PostgresRepository:
    name = "postgres"

    async def close(self) -> None:
        """Nothing to do: the connection pool belongs to the process (closed by the CLI on exit), so concurrent work in
        the long-running service (`agent start`) keeps it."""
        return None

    # --- pages and queries -----------------------------------------------------------------------------------

    async def page_seen(self, key: str) -> bool:
        return (
            await db.fetchrow("select 1 from lead_signals where content_hash=$1", key) is not None
        )

    async def record_page(self, key, url, kind, extracted, lead_id=None) -> None:
        await db.execute(
            """insert into lead_signals (company_id, kind, source_url, raw_text, extracted, content_hash)
               values ($1::uuid, $2, $3, null, $4::jsonb, $5) on conflict (content_hash) do nothing""",
            lead_id, kind, url, db.dumps(extracted), key,
        )  # fmt: skip

    async def query_last_run(self, query: str) -> datetime | None:
        row = await db.fetchrow("select last_run_at from search_queries where query=$1", query)
        return row["last_run_at"] if row else None

    async def record_query(self, query, family, engine, results, opened) -> None:
        await db.execute(
            """insert into search_queries (query, family, engine, results, opened, last_run_at)
               values ($1,$2,$3,$4,$5, now()) on conflict (query) do update set engine=excluded.engine,
               results=excluded.results, opened=excluded.opened, last_run_at=now()""",
            query, family, engine, results, opened,
        )  # fmt: skip

    # --- leads ---------------------------------------------------------------------------------------------------

    _LEAD_Q = """select co.*,
        (select row_to_json(c) from (select id, name, role, email, linkedin, source_url, confidence, rank
           from contacts where company_id = co.id order by coalesce(rank, 99), confidence desc nulls last
           limit 1) c) as best_contact,
        (select e.id from emails e join contacts c on c.id = e.contact_id
          where c.company_id = co.id and e.step = 1 and e.campaign_id =
                (select id from campaigns where name = 'daily-outreach') limit 1) as intro_id
        from companies co"""

    async def _lead(self, r) -> Lead:
        lead = Lead(
            lead_id=str(r["id"]), company_name=r["name"], company_website=r["domain"], name_key=r["name_key"] or "",
            industry=r["industry"] or "", product=r["product"] or "",
            project_description=r["project_summary"] or "", opportunity_description=r["opportunity"] or "",
            technical_requirements=list(r["technologies"] or []), project_signal=r["project_signal"] or "none",
            page_type=r["page_type"] or "other", location=r["location"] or "", lead_score=r["lead_score"] or 0,
            result_type=r["result_type"] or "UNKNOWN", customer_tier=r["customer_tier"] or "none",
            score=ScoreCard.model_validate(_json(r["score"])) if r["score"] else None,
            evidence=[Evidence(**e) for e in _json(r["evidence"])],
            source_urls=list(r["source_urls"] or []) or ([r["source_url"]] if r["source_url"] else []),
            qualification_notes=list(_json(r["qualification_notes"])),
            status=LeadStatus(r["lead_status"]), created_at=r["created_at"], updated_at=r["updated_at"],
        )  # fmt: skip
        if r["best_contact"]:
            c: dict = (
                json.loads(r["best_contact"])
                if isinstance(r["best_contact"], str)
                else r["best_contact"]
            )
            lead.contact = Contact(
                id=str(c["id"]), name=c["name"] or "", role=c["role"] or "", email=c["email"] or "",
                linkedin=c["linkedin"] or "", source=c["source_url"], confidence=float(c["confidence"] or 0),
                rank=c["rank"] if c["rank"] is not None else 99,
            )  # fmt: skip
        if r["intro_id"]:
            lead.email_draft_id = str(r["intro_id"])
            ap = await self.get_approval(lead.email_draft_id)
            lead.approval_status = ap.status if ap else None
        return lead

    async def find_lead(self, *, domain: str = "", name_key: str = "") -> Lead | None:
        row = None
        if domain:
            row = await db.fetchrow(f"{self._LEAD_Q} where co.domain=$1", domain)
        if row is None and name_key:
            row = await db.fetchrow(f"{self._LEAD_Q} where co.name_key=$1 limit 1", name_key)
        return await self._lead(row) if row else None

    async def get_lead(self, lead_id: str) -> Lead | None:
        row = await db.fetchrow(f"{self._LEAD_Q} where co.id=$1::uuid", lead_id)
        return await self._lead(row) if row else None

    @staticmethod
    def _values(lead: Lead) -> tuple:
        return (
            lead.company_name, lead.name_key, lead.industry or None, lead.product or None,
            lead.project_description or None, lead.opportunity_description or None, lead.technical_requirements,
            lead.project_signal, lead.page_type, lead.location or None, lead.lead_score,
            lead.score.model_dump_json() if lead.score else None,
            db.dumps([e.model_dump() for e in lead.evidence]), lead.source_urls, db.dumps(lead.qualification_notes),
            lead.lead_score / 100, (lead.opportunity_description or lead.project_description)[:400] or None,
            lead.result_type, lead.customer_tier,
        )  # fmt: skip

    _SET = """name=$1, name_key=$2, industry=$3, product=$4, project_summary=$5, opportunity=$6, technologies=$7,
        project_signal=$8, page_type=$9, location=$10, lead_score=$11, score=$12::jsonb, evidence=$13::jsonb,
        source_urls=$14, qualification_notes=$15::jsonb, fit_score=$16, fit_reason=$17, result_type=$18,
        customer_tier=$19"""

    async def insert_lead(self, lead: Lead, *, source: str) -> str:
        row = await db.fetchrow(
            """insert into companies (name, name_key, industry, product, project_summary, opportunity, technologies,
                   project_signal, page_type, location, lead_score, score, evidence, source_urls,
                   qualification_notes, fit_score, fit_reason, result_type, customer_tier, domain, website, source,
                   source_url, status, lead_status)
               values ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12::jsonb,$13::jsonb,$14,$15::jsonb,$16,$17,
                       $18,$19,$20,$21,$22,$23,'candidate','DISCOVERED')
               returning id""",
            *self._values(lead), lead.company_website, f"https://{lead.company_website}", source,
            lead.source_urls[0] if lead.source_urls else None,
        )  # fmt: skip
        assert row is not None  # noqa: S101 - insert ... returning always yields a row
        lead.lead_id, lead.status = str(row["id"]), LeadStatus.DISCOVERED
        return lead.lead_id

    async def save_lead(self, lead: Lead) -> None:
        await db.execute(
            f"update companies set {self._SET}, updated_at=now() where id=$20::uuid",
            *self._values(lead), lead.lead_id,
        )  # fmt: skip

    async def set_status(self, lead_id: str, new: LeadStatus, *, note: str = "") -> bool:
        row = await db.fetchrow(
            """update companies set lead_status=$2, status_note=$3, updated_at=now(),
                   status = case when status in ('suppressed','engaged','closed') then status else $4 end
                where id=$1::uuid and lead_status = any($5::text[]) returning id""",
            lead_id, new.value, note or None, _LEGACY[new], [s.value for s in sources_of(new)],
        )  # fmt: skip
        return row is not None

    async def leads_with_status(self, statuses: list[LeadStatus], limit: int = 50) -> list[Lead]:
        rows = await db.fetch(
            f"{self._LEAD_Q} where co.lead_status = any($1::text[]) order by co.updated_at limit $2",
            [s.value for s in statuses], limit,
        )  # fmt: skip
        return [await self._lead(r) for r in rows]

    async def drafted_since(self, since: datetime) -> int:
        row = await db.fetchrow(
            """select count(*) n from emails where step=1 and created_at >= $1
                 and campaign_id=(select id from campaigns where name='daily-outreach')""",
            since,
        )
        return int(row["n"]) if row else 0

    async def set_manual(self, lead_id: str, form_url: str | None, reason: str | None) -> None:
        await db.execute(
            """update companies set contact_form_url=coalesce($2, contact_form_url),
                   manual_reason=coalesce($3, manual_reason), updated_at=now() where id=$1::uuid""",
            lead_id, form_url, reason,
        )  # fmt: skip

    async def record_contact_attempt(self, lead_id: str) -> int:
        row = await db.fetchrow(
            """update companies set contact_attempts=contact_attempts+1, updated_at=now()
                where id=$1::uuid returning contact_attempts""",
            lead_id,
        )
        return int(row["contact_attempts"]) if row else 0

    async def leads_needing_contact(self, max_attempts: int, limit: int) -> list[Lead]:
        rows = await db.fetch(
            f"""{self._LEAD_Q} where co.lead_status='QUALIFIED' and co.contact_attempts < $1
                 order by co.updated_at limit $2""",
            max_attempts, limit,
        )  # fmt: skip
        return [await self._lead(r) for r in rows]

    # --- contacts and emails ---------------------------------------------------------------------------------

    async def save_contact(self, lead_id: str, contact: Contact) -> str:
        row = await db.fetchrow(
            """insert into contacts (company_id, name, role, email, linkedin, source_url, confidence, rank, verification)
               values ($1::uuid,$2,$3,$4,$5,$6,$7,$8,'unverified')
               on conflict (company_id, email) do update set collected_at=now() returning id""",
            lead_id, contact.name or None, contact.role or None, contact.email.lower() or None,
            contact.linkedin or None, contact.source, contact.confidence, contact.rank,
        )  # fmt: skip
        assert row is not None  # noqa: S101
        return str(row["id"])

    async def contacts_for(self, lead_id: str) -> list[Contact]:
        rows = await db.fetch(
            "select * from contacts where company_id=$1::uuid order by coalesce(rank, 99), confidence desc nulls last",
            lead_id,
        )
        return [
            Contact(id=str(r["id"]), name=r["name"] or "", role=r["role"] or "", email=r["email"] or "",
                    linkedin=r["linkedin"] or "", source=r["source_url"], confidence=float(r["confidence"] or 0),
                    rank=r["rank"] if r["rank"] is not None else 99)
            for r in rows
        ]  # fmt: skip

    async def _campaign(self) -> UUID:
        row = await db.fetchrow("select id from campaigns where name='daily-outreach'")
        if row:
            return row["id"]
        row = await db.fetchrow(
            "insert into campaigns (name, steps) values ('daily-outreach', 2) returning id"
        )
        assert row is not None  # noqa: S101
        return row["id"]

    _EMAIL_Q = """select e.*, c.company_id, c.email as to_addr from emails e
                    join contacts c on c.id = e.contact_id"""

    @staticmethod
    def _email(r) -> EmailDraft:
        return EmailDraft(email_id=str(r["id"]), lead_id=str(r["company_id"]), contact_id=str(r["contact_id"]),
                          to=r["to_addr"] or "", subject=r["subject"] or "", body=r["body"] or "",
                          review_note=r["review_note"], status=r["status"], step=r["step"],
                          attempts=r["attempts"], in_reply_to=r["in_reply_to"])  # fmt: skip

    async def save_email_draft(self, lead_id, contact_id, subject, body, note) -> str | None:
        row = await db.fetchrow(
            """insert into emails (contact_id, campaign_id, step, status, subject, body, review_note, idempotency_key)
               values ($1::uuid,$2,1,'drafted',$3,$4,$5,$6)
               on conflict (contact_id, campaign_id, step) do nothing returning id""",
            contact_id, await self._campaign(), subject, body, note, f"intro-{contact_id}",
        )  # fmt: skip
        return str(row["id"]) if row else None

    async def email_for_lead(self, lead_id: str) -> EmailDraft | None:
        row = await db.fetchrow(
            f"""{self._EMAIL_Q} where c.company_id=$1::uuid and e.step=1
                 and e.campaign_id=(select id from campaigns where name='daily-outreach')""",
            lead_id,
        )
        return self._email(row) if row else None

    async def get_email(self, email_id: str) -> EmailDraft | None:
        row = await db.fetchrow(f"{self._EMAIL_Q} where e.id=$1::uuid", email_id)
        return self._email(row) if row else None

    async def drafts(self, limit: int = 100) -> list[EmailDraft]:
        rows = await db.fetch(
            f"{self._EMAIL_Q} where e.status='drafted' order by e.created_at limit $1", limit
        )
        return [self._email(r) for r in rows]

    # --- approval ----------------------------------------------------------------------------------------------

    async def open_approval(self, email_id: str, channel: str) -> None:
        await db.execute(
            """insert into approvals (kind, ref_id, status, slack_channel) values ('email', $1::uuid, 'pending', $2)
               on conflict (kind, ref_id) where status = 'pending' do nothing""",
            email_id, channel,
        )  # fmt: skip

    async def set_approval_message(self, email_id: str, channel: str, message_id: str) -> None:
        await db.execute(
            """update approvals set slack_channel=$2, slack_ts=$3
                where kind='email' and ref_id=$1::uuid and status='pending'""",
            email_id, channel, message_id,
        )  # fmt: skip

    async def get_approval(self, email_id: str) -> Approval | None:
        r = await db.fetchrow(
            """select a.*, c.company_id from approvals a left join emails e on e.id=a.ref_id
                 left join contacts c on c.id=e.contact_id
                where a.kind='email' and a.ref_id=$1::uuid order by a.created_at desc limit 1""",
            email_id,
        )
        if not r:
            return None
        ok, no = r["status"] == "approved", r["status"] == "rejected"
        return Approval(
            email_id=email_id, lead_id=str(r["company_id"]) if r["company_id"] else None, status=r["status"],
            slack_channel=r["slack_channel"], slack_message_id=r["slack_ts"],
            approved_by=r["decided_by"] if ok else None, approved_at=r["decided_at"] if ok else None,
            rejected_by=r["decided_by"] if no else None, rejected_at=r["decided_at"] if no else None,
        )  # fmt: skip

    async def decide_email(
        self, email_id: str, approve: bool, by: str, note: str | None = None
    ) -> str:
        row = await db.fetchrow(
            "select decide_email($1::uuid, $2, $3, $4) as d", email_id, approve, by, note
        )
        return row["d"] if row else "unknown"

    # --- sending (the gate) -----------------------------------------------------------------------------------

    _GATE = """e.status = 'approved' and c.email is not null
        and exists (select 1 from approvals a where a.kind='email' and a.ref_id=e.id and a.status='approved')
        and (not (e.step = 1 and e.campaign_id = (select id from campaigns where name='daily-outreach'))
             or co.lead_status = 'APPROVED')"""

    async def claim_sendable(self, limit: int) -> list[EmailDraft]:
        rows = await db.fetch(
            f"""update emails set status='sending', updated_at=now() where id in (
                  select e.id from emails e join contacts c on c.id=e.contact_id join companies co on co.id=c.company_id
                   where {self._GATE} order by e.created_at limit $1 for update of e skip locked)
                returning id""",
            limit,
        )
        return [e for r in rows if (e := await self.get_email(str(r["id"])))]

    async def sendable_problem(self, email_id: str) -> str | None:
        row = await db.fetchrow(
            """select e.status, c.email, co.lead_status,
                      case when e.step = 1 and e.campaign_id = (select id from campaigns where name='daily-outreach')
                           then 1 else 2 end as step,
                      (select count(*) from approvals a where a.kind='email' and a.ref_id=e.id
                          and a.status='approved') as ok
                 from emails e join contacts c on c.id=e.contact_id join companies co on co.id=c.company_id
                where e.id=$1::uuid""",
            email_id,
        )
        return _problem(row)

    async def release_claim(self, email_id: str) -> None:
        await db.execute(
            "update emails set status='approved', updated_at=now() where id=$1::uuid and status='sending'",
            email_id,
        )

    async def thread_id_for(self, email_id: str) -> str | None:
        """The Gmail conversation this email continues: the reply it answers, else the mail its In-Reply-To names."""
        row = await db.fetchrow(
            """select coalesce(
                 (select r.gmail_thread_id from replies r where r.id = e.reply_id),
                 (select o.gmail_thread_id from emails o where o.message_id = e.in_reply_to
                     and o.gmail_thread_id is not null limit 1)) as t
                 from emails e where e.id = $1::uuid""",
            email_id,
        )
        return row["t"] if row else None

    async def mark_sent(self, email_id, message_id, provider_id, mailbox, thread_id=None) -> None:
        await db.execute(
            """update emails set status='sent', sent_at=now(), message_id=$2, provider_id=$3, mailbox=$4,
                   gmail_message_id=nullif($3, 'dry-run'), gmail_thread_id=$5, updated_at=now()
                where id=$1::uuid and status='sending'""",
            email_id, message_id, provider_id, mailbox, thread_id,
        )  # fmt: skip
        await db.execute(
            """update companies set lead_status='SENT', updated_at=now() where lead_status='APPROVED' and id =
                 (select c.company_id from emails e join contacts c on c.id=e.contact_id where e.id=$1::uuid)""",
            email_id,
        )

    async def mark_send_failed(self, email_id: str, error: str, *, final: bool) -> None:
        await db.execute(
            """update emails set status=$2, attempts=attempts+1, last_error=$3, updated_at=now()
                where id=$1::uuid and status='sending'""",
            email_id, "expired" if final else "approved", error[:500],
        )  # fmt: skip
        if final:
            await db.execute(
                """update companies set lead_status='FAILED', updated_at=now() where lead_status='APPROVED' and id =
                     (select c.company_id from emails e join contacts c on c.id=e.contact_id where e.id=$1::uuid)""",
                email_id,
            )

    async def is_suppressed(self, addr: str) -> bool:
        row = await db.fetchrow("select is_suppressed($1) as s", addr)
        return bool(row and row["s"])

    async def try_increment_send(self, mailbox: str, cap: int) -> bool:
        row = await db.fetchrow("select try_increment_send($1,$2) as ok", mailbox, cap)
        return bool(row and row["ok"])
