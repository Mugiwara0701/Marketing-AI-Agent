"""SQLite repository: dry-runs and tests. Same model and the same gate as production, in a local file."""

import json
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path

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

_SCHEMA = """
create table if not exists companies (
  id text primary key, name text not null, domain text not null unique, name_key text,
  lead_status text not null default 'DISCOVERED', industry text, product text, project_summary text,
  opportunity text, technologies text, project_signal text, page_type text, location text,
  lead_score integer default 0, score text, evidence text, source_urls text, notes text,
  source text, contact_form_url text, manual_reason text, status_note text,
  created_at text not null, updated_at text not null
);
create index if not exists companies_name_key on companies (name_key);
create table if not exists contacts (
  id text primary key, company_id text not null references companies (id), name text, role text,
  email text, linkedin text, source_url text not null, confidence real, rank integer,
  unique (company_id, email)
);
create table if not exists emails (
  id text primary key, contact_id text not null references contacts (id), step integer not null default 1,
  status text not null default 'drafted', subject text, body text, review_note text,
  message_id text, provider_id text, mailbox text, attempts integer not null default 0, last_error text,
  sent_at text, created_at text not null, updated_at text not null, unique (contact_id, step)
);
create table if not exists approvals (
  id text primary key, kind text not null, ref_id text not null, status text not null default 'pending',
  slack_channel text, slack_ts text, decided_by text, decided_at text, decision_note text, created_at text not null
);
create unique index if not exists approvals_one_pending on approvals (kind, ref_id) where status = 'pending';
create table if not exists lead_signals (
  content_hash text primary key, company_id text, kind text, source_url text, extracted text, created_at text not null
);
create table if not exists search_queries (
  query text primary key, family text, engine text, results integer, opened integer, last_run_at text not null
);
create table if not exists suppression_list (email text primary key, reason text);
create table if not exists send_counters (day text, mailbox text, sent integer, primary key (day, mailbox));
"""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _dt(v: str | None) -> datetime | None:
    return datetime.fromisoformat(v) if v else None


def _j(v) -> str:
    return json.dumps(v, default=str)


class SqliteRepository:
    name = "sqlite"

    def __init__(self, path: str = ":memory:") -> None:
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.db = sqlite3.connect(
            path, isolation_level=None
        )  # autocommit; explicit BEGIN where needed
        self.db.row_factory = sqlite3.Row
        self.db.execute("pragma foreign_keys = on")
        self.db.executescript(_SCHEMA)

    async def close(self) -> None:
        self.db.close()

    def _one(self, sql: str, *args) -> sqlite3.Row | None:
        return self.db.execute(sql, args).fetchone()

    # --- pages and queries ---------------------------------------------------------------------------------

    async def page_seen(self, key: str) -> bool:
        return self._one("select 1 from lead_signals where content_hash=?", key) is not None

    async def record_page(self, key, url, kind, extracted, lead_id=None) -> None:
        self.db.execute(
            "insert or ignore into lead_signals values (?,?,?,?,?,?)",
            (key, lead_id, kind, url, _j(extracted), _now()),
        )

    async def query_last_run(self, query: str) -> datetime | None:
        row = self._one("select last_run_at from search_queries where query=?", query)
        return _dt(row["last_run_at"]) if row else None

    async def record_query(self, query, family, engine, results, opened) -> None:
        self.db.execute(
            "insert into search_queries values (?,?,?,?,?,?) on conflict (query) do update set "
            "engine=excluded.engine, results=excluded.results, opened=excluded.opened, last_run_at=excluded.last_run_at",
            (query, family, engine, results, opened, _now()),
        )

    # --- leads -------------------------------------------------------------------------------------------------

    def _lead(self, r: sqlite3.Row) -> Lead:
        lead = Lead(
            lead_id=r["id"], company_name=r["name"], company_website=r["domain"], name_key=r["name_key"] or "",
            industry=r["industry"] or "", product=r["product"] or "",
            project_description=r["project_summary"] or "", opportunity_description=r["opportunity"] or "",
            technical_requirements=json.loads(r["technologies"] or "[]"),
            project_signal=r["project_signal"] or "none", page_type=r["page_type"] or "other",
            location=r["location"] or "", lead_score=r["lead_score"] or 0,
            score=ScoreCard(**json.loads(r["score"])) if r["score"] else None,
            evidence=[Evidence(**e) for e in json.loads(r["evidence"] or "[]")],
            source_urls=json.loads(r["source_urls"] or "[]"),
            qualification_notes=json.loads(r["notes"] or "[]"),
            status=LeadStatus(r["lead_status"]),
            created_at=_dt(r["created_at"]), updated_at=_dt(r["updated_at"]),
        )  # fmt: skip
        contacts = self.db.execute(
            "select * from contacts where company_id=? order by rank, confidence desc", (r["id"],)
        ).fetchall()
        if contacts:
            lead.contact = self._contact(contacts[0])
        if em := self._one(
            "select e.id, e.status from emails e join contacts c on c.id=e.contact_id "
            "where c.company_id=? and e.step=1",
            r["id"],
        ):
            lead.email_draft_id = em["id"]
            if ap := self._one(
                "select status from approvals where kind='email' and ref_id=? order by created_at desc",
                em["id"],
            ):
                lead.approval_status = ap["status"]
        return lead

    async def find_lead(self, *, domain: str = "", name_key: str = "") -> Lead | None:
        row = None
        if domain:
            row = self._one("select * from companies where domain=?", domain)
        if row is None and name_key:
            row = self._one("select * from companies where name_key=?", name_key)
        return self._lead(row) if row else None

    async def get_lead(self, lead_id: str) -> Lead | None:
        row = self._one("select * from companies where id=?", lead_id)
        return self._lead(row) if row else None

    def _lead_values(self, lead: Lead) -> tuple:
        return (
            lead.company_name, lead.name_key, lead.industry, lead.product, lead.project_description,
            lead.opportunity_description, _j(lead.technical_requirements), lead.project_signal, lead.page_type,
            lead.location, lead.lead_score, lead.score.model_dump_json() if lead.score else None,
            _j([e.model_dump() for e in lead.evidence]), _j(lead.source_urls), _j(lead.qualification_notes),
        )  # fmt: skip

    async def insert_lead(self, lead: Lead, *, source: str) -> str:
        lead_id = str(uuid.uuid4())
        now = _now()
        self.db.execute(
            "insert into companies (id, domain, source, lead_status, created_at, updated_at, name, name_key, industry,"
            " product, project_summary, opportunity, technologies, project_signal, page_type, location, lead_score,"
            " score, evidence, source_urls, notes) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                lead_id,
                lead.company_website,
                source,
                LeadStatus.DISCOVERED.value,
                now,
                now,
                *self._lead_values(lead),
            ),
        )
        lead.lead_id, lead.status = lead_id, LeadStatus.DISCOVERED
        return lead_id

    async def save_lead(self, lead: Lead) -> None:
        self.db.execute(
            "update companies set name=?, name_key=?, industry=?, product=?, project_summary=?, opportunity=?,"
            " technologies=?, project_signal=?, page_type=?, location=?, lead_score=?, score=?, evidence=?,"
            " source_urls=?, notes=?, updated_at=? where id=?",
            (*self._lead_values(lead), _now(), lead.lead_id),
        )

    async def set_status(self, lead_id: str, new: LeadStatus, *, note: str = "") -> bool:
        allowed = [s.value for s in sources_of(new)]
        marks = ",".join("?" * len(allowed))
        cur = self.db.execute(
            f"update companies set lead_status=?, status_note=?, updated_at=? where id=? and lead_status in ({marks})",
            (new.value, note or None, _now(), lead_id, *allowed),
        )
        return cur.rowcount == 1

    async def leads_with_status(self, statuses: list[LeadStatus], limit: int = 50) -> list[Lead]:
        marks = ",".join("?" * len(statuses))
        rows = self.db.execute(
            f"select * from companies where lead_status in ({marks}) order by updated_at limit ?",
            (*[s.value for s in statuses], limit),
        ).fetchall()
        return [self._lead(r) for r in rows]

    async def drafted_since(self, since: datetime) -> int:
        row = self._one(
            "select count(*) n from emails where step=1 and created_at >= ?", since.isoformat()
        )
        return int(row["n"]) if row else 0

    async def set_manual(self, lead_id: str, form_url: str | None, reason: str | None) -> None:
        self.db.execute(
            "update companies set contact_form_url=coalesce(?, contact_form_url), "
            "manual_reason=coalesce(?, manual_reason), updated_at=? where id=?",
            (form_url, reason, _now(), lead_id),
        )

    # --- contacts and emails ---------------------------------------------------------------------------------

    @staticmethod
    def _contact(r: sqlite3.Row) -> Contact:
        return Contact(id=r["id"], name=r["name"] or "", role=r["role"] or "", email=r["email"] or "",
                       linkedin=r["linkedin"] or "", source=r["source_url"], confidence=r["confidence"] or 0,
                       rank=r["rank"] if r["rank"] is not None else 99)  # fmt: skip

    async def save_contact(self, lead_id: str, contact: Contact) -> str:
        email = contact.email.lower() or None
        row = self._one("select id from contacts where company_id=? and email is ?", lead_id, email)
        if row:
            return row["id"]
        cid = str(uuid.uuid4())
        self.db.execute(
            "insert into contacts values (?,?,?,?,?,?,?,?,?)",
            (cid, lead_id, contact.name or None, contact.role or None, email, contact.linkedin or None,
             contact.source, contact.confidence, contact.rank),
        )  # fmt: skip
        return cid

    async def contacts_for(self, lead_id: str) -> list[Contact]:
        rows = self.db.execute(
            "select * from contacts where company_id=? order by rank, confidence desc", (lead_id,)
        ).fetchall()
        return [self._contact(r) for r in rows]

    _EMAIL_Q = "select e.*, c.company_id, c.email as to_addr from emails e join contacts c on c.id=e.contact_id"

    @staticmethod
    def _email(r: sqlite3.Row) -> EmailDraft:
        return EmailDraft(email_id=r["id"], lead_id=r["company_id"], contact_id=r["contact_id"],
                          to=r["to_addr"] or "", subject=r["subject"] or "", body=r["body"] or "",
                          review_note=r["review_note"], status=r["status"], step=r["step"],
                          attempts=r["attempts"])  # fmt: skip

    async def save_email_draft(self, lead_id, contact_id, subject, body, note) -> str | None:
        eid = str(uuid.uuid4())
        now = _now()
        cur = self.db.execute(
            "insert or ignore into emails (id, contact_id, step, status, subject, body, review_note, created_at,"
            " updated_at) values (?,?,1,'drafted',?,?,?,?,?)",
            (eid, contact_id, subject, body, note, now, now),
        )
        return eid if cur.rowcount == 1 else None

    async def email_for_lead(self, lead_id: str) -> EmailDraft | None:
        row = self._one(f"{self._EMAIL_Q} where c.company_id=? and e.step=1", lead_id)
        return self._email(row) if row else None

    async def get_email(self, email_id: str) -> EmailDraft | None:
        row = self._one(f"{self._EMAIL_Q} where e.id=?", email_id)
        return self._email(row) if row else None

    async def drafts(self, limit: int = 100) -> list[EmailDraft]:
        rows = self.db.execute(
            f"{self._EMAIL_Q} where e.status='drafted' order by e.created_at limit ?", (limit,)
        ).fetchall()
        return [self._email(r) for r in rows]

    # --- approval ----------------------------------------------------------------------------------------------

    async def open_approval(self, email_id: str, channel: str) -> None:
        self.db.execute(
            "insert or ignore into approvals (id, kind, ref_id, status, slack_channel, created_at) "
            "values (?, 'email', ?, 'pending', ?, ?)",
            (str(uuid.uuid4()), email_id, channel, _now()),
        )

    async def set_approval_message(self, email_id: str, channel: str, message_id: str) -> None:
        self.db.execute(
            "update approvals set slack_channel=?, slack_ts=? where kind='email' and ref_id=? and status='pending'",
            (channel, message_id, email_id),
        )

    async def get_approval(self, email_id: str) -> Approval | None:
        r = self._one(
            "select a.*, c.company_id from approvals a left join emails e on e.id=a.ref_id "
            "left join contacts c on c.id=e.contact_id where a.kind='email' and a.ref_id=? "
            "order by a.created_at desc",
            email_id,
        )
        if not r:
            return None
        ok, decided = r["status"] == "approved", r["status"] == "rejected"
        return Approval(
            email_id=email_id, lead_id=r["company_id"], status=r["status"], slack_channel=r["slack_channel"],
            slack_message_id=r["slack_ts"],
            approved_by=r["decided_by"] if ok else None, approved_at=_dt(r["decided_at"]) if ok else None,
            rejected_by=r["decided_by"] if decided else None, rejected_at=_dt(r["decided_at"]) if decided else None,
        )  # fmt: skip

    async def decide_email(
        self, email_id: str, approve: bool, by: str, note: str | None = None
    ) -> str:
        """Same contract as the Postgres function decide_email(): exactly one decision per draft."""
        self.db.execute("begin immediate")
        try:
            e = self._one(
                "select e.status, e.step, c.company_id from emails e join contacts c on c.id=e.contact_id "
                "where e.id=?",
                email_id,
            )
            if e is None:
                self.db.execute("rollback")
                return "unknown"
            if e["status"] != "drafted":
                self.db.execute("rollback")
                return "already_decided"
            now = _now()
            self.db.execute(
                "update emails set status=?, updated_at=? where id=?",
                ("approved" if approve else "skipped", now, email_id),
            )
            decision = "approved" if approve else "rejected"
            cur = self.db.execute(
                "update approvals set status=?, decided_by=?, decided_at=?, decision_note=? "
                "where kind='email' and ref_id=? and status='pending'",
                (decision, by, now, note, email_id),
            )
            if cur.rowcount == 0:
                self.db.execute(
                    "insert into approvals (id, kind, ref_id, status, decided_by, decided_at, decision_note, created_at)"
                    " values (?, 'email', ?, ?, ?, ?, ?, ?)",
                    (str(uuid.uuid4()), email_id, decision, by, now, note, now),
                )
            if e["step"] == 1:
                new = LeadStatus.APPROVED if approve else LeadStatus.REJECTED
                self.db.execute(
                    "update companies set lead_status=?, updated_at=? where id=? "
                    "and lead_status in ('EMAIL_DRAFTED','PENDING_APPROVAL')",
                    (new.value, now, e["company_id"]),
                )
            self.db.execute("commit")
            return decision
        except BaseException:
            self.db.execute("rollback")
            raise

    # --- sending -------------------------------------------------------------------------------------------------

    _GATE = """e.status = ? and c.email is not null
        and exists (select 1 from approvals a where a.kind='email' and a.ref_id=e.id and a.status='approved')
        and (e.step <> 1 or co.lead_status = 'APPROVED')"""

    async def claim_sendable(self, limit: int) -> list[EmailDraft]:
        self.db.execute("begin immediate")
        try:
            ids = [
                r["id"]
                for r in self.db.execute(
                    "select e.id from emails e join contacts c on c.id=e.contact_id "
                    f"join companies co on co.id=c.company_id where {self._GATE} order by e.created_at limit ?",
                    ("approved", limit),
                ).fetchall()
            ]
            for i in ids:
                self.db.execute(
                    "update emails set status='sending', updated_at=? where id=?", (_now(), i)
                )
            self.db.execute("commit")
        except BaseException:
            self.db.execute("rollback")
            raise
        return [e for i in ids if (e := await self.get_email(i))]

    async def sendable_problem(self, email_id: str) -> str | None:
        row = self._one(
            "select e.status, e.step, c.email, co.lead_status, "
            "(select count(*) from approvals a where a.kind='email' and a.ref_id=e.id and a.status='approved') ok "
            "from emails e join contacts c on c.id=e.contact_id join companies co on co.id=c.company_id where e.id=?",
            email_id,
        )
        return _problem(row)

    async def release_claim(self, email_id: str) -> None:
        self.db.execute(
            "update emails set status='approved', updated_at=? where id=? and status='sending'",
            (_now(), email_id),
        )

    async def mark_sent(self, email_id, message_id, provider_id, mailbox) -> None:
        now = _now()
        self.db.execute(
            "update emails set status='sent', sent_at=?, message_id=?, provider_id=?, mailbox=?, updated_at=? "
            "where id=? and status='sending'",
            (now, message_id, provider_id, mailbox, now, email_id),
        )
        self.db.execute(
            "update companies set lead_status='SENT', updated_at=? where lead_status='APPROVED' and id="
            "(select c.company_id from emails e join contacts c on c.id=e.contact_id where e.id=? and e.step=1)",
            (now, email_id),
        )

    async def mark_send_failed(self, email_id: str, error: str, *, final: bool) -> None:
        now = _now()
        self.db.execute(
            "update emails set status=?, attempts=attempts+1, last_error=?, updated_at=? where id=? and status='sending'",
            ("expired" if final else "approved", error[:500], now, email_id),
        )
        if final:
            self.db.execute(
                "update companies set lead_status='FAILED', updated_at=? where lead_status='APPROVED' and id="
                "(select c.company_id from emails e join contacts c on c.id=e.contact_id where e.id=? and e.step=1)",
                (now, email_id),
            )

    async def is_suppressed(self, addr: str) -> bool:
        return (
            self._one("select 1 from suppression_list where email=?", addr.strip().lower())
            is not None
        )

    async def try_increment_send(self, mailbox: str, cap: int) -> bool:
        day = datetime.now(UTC).date().isoformat()
        self.db.execute("insert or ignore into send_counters values (?,?,0)", (day, mailbox))
        cur = self.db.execute(
            "update send_counters set sent=sent+1 where day=? and mailbox=? and sent < ?",
            (day, mailbox, cap),
        )
        return cur.rowcount == 1


def _problem(row) -> str | None:
    """Why an email must not be sent (None = it may). Shared by both repositories."""
    if row is None:
        return "unknown email"
    if row["status"] not in ("approved", "sending"):
        return f"email status is {row['status']}, not approved"
    if not row["ok"]:
        return "no recorded human approval"
    if row["step"] == 1 and row["lead_status"] != "APPROVED":
        return f"lead status is {row['lead_status']}, not APPROVED"
    if not row["email"]:
        return "contact has no email address"
    return None
