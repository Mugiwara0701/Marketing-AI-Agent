"""Human approval gate for outreach emails (CLI). Nothing is sent until a person approves it."""

from agentkit import db
from agentkit.config import env

_Q = """select e.id, e.subject, e.body, e.review_note, e.created_at, c.email, c.name, c.role,
              co.name as company, co.domain, co.project_summary, co.technologies, co.source_url
         from emails e join contacts c on c.id = e.contact_id join companies co on co.id = c.company_id"""


async def list_drafts() -> list:
    return await db.fetch(
        f"{_Q} where e.status='drafted' order by e.created_at",
    )


async def show(email_id: str):
    return await db.fetchrow(f"{_Q} where e.id=$1::uuid", email_id)


async def decide(ids: list[str], approve: bool) -> int:
    who = env("USER", "cli") or "cli"
    status = "approved" if approve else "skipped"
    n = 0
    for i in ids:
        row = await db.fetchrow(
            "update emails set status=$2, updated_at=now() where id=$1::uuid and status='drafted' returning id",
            i,
            status,
        )
        if row:
            n += 1
            await db.execute(
                "insert into approvals (kind, ref_id, status, decided_by, decided_at) values ('email',$1,$2,$3,now())",
                row["id"], "approved" if approve else "rejected", who,
            )  # fmt: skip
    return n
