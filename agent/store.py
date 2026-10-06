"""Database reads/writes for blog, replies, follow-ups and manual contact (thin wrappers over agentkit.db).
Leads, contacts, intro emails and approvals live in agent.leadgen.repository."""

from datetime import date
from uuid import UUID

from agentkit import db


async def _campaign(name: str, steps: int) -> UUID:
    row = await db.fetchrow("select id from campaigns where name=$1", name)
    if row:
        return row["id"]
    row = await db.fetchrow(
        "insert into campaigns (name, steps) values ($1,$2) returning id", name, steps
    )
    assert row is not None  # noqa: S101
    return row["id"]


async def campaign_id() -> UUID:
    return await _campaign("daily-outreach", 1)


async def reply_campaign_id() -> UUID:
    """Replies we send live in their own campaign, so they never collide with the intro's step."""
    return await _campaign("replies", 1)


async def recent_post_titles(limit: int = 60) -> list[str]:
    rows = await db.fetch(
        "select title from content_posts where title is not null order by created_at desc limit $1",
        limit,
    )
    return [r["title"] for r in rows]


async def blog_exists(day: date) -> bool:
    return (
        await db.fetchrow("select 1 from content_posts where idempotency_key=$1", f"blog-{day}")
        is not None
    )


async def save_blog(day: date, topic, post, metadata: dict, status: str) -> UUID:
    t = await db.fetchrow(
        "insert into topics (title, brief, status) values ($1,$2,'used') returning id",
        topic.title, topic.angle,
    )  # fmt: skip
    assert t is not None  # noqa: S101
    row = await db.fetchrow(
        """insert into content_posts (topic_id, title, body_md, status, idempotency_key, metadata, tags)
           values ($1,$2,$3,$4,$5,$6::jsonb,$7)
           on conflict (idempotency_key) do nothing returning id""",
        t["id"], post.title, post.body_markdown, status, f"blog-{day}", db.dumps(metadata), post.tags,
    )  # fmt: skip
    assert row is not None  # noqa: S101
    return row["id"]


async def company_with_form(ref: str):
    """A company that has a contact form, by domain or id."""
    return await db.fetchrow(
        "select id, name, domain, location, technologies, project_summary, contact_form_url "
        "from companies where contact_form_url is not null and (domain=$1 or id::text=$1)",
        ref,
    )


async def companies_to_contact_manually() -> list:
    """Companies we could not read automatically: a bot check on their site, or only a contact form."""
    return await db.fetch(
        "select name, domain, manual_reason, contact_form_url, project_summary from companies "
        "where (manual_reason is not null or contact_form_url is not null) and lead_status = 'QUALIFIED' "
        "order by created_at desc"
    )


async def save_variant(  # noqa: PLR0917
    post_id: UUID, platform: str, title: str, body: str, tags: list[str], note: str | None
) -> None:
    await db.execute(
        """insert into content_variants (post_id, platform, title, body, tags, review_note)
           values ($1,$2,$3,$4,$5,$6)
           on conflict (post_id, platform) do update
             set title=excluded.title, body=excluded.body, tags=excluded.tags, review_note=excluded.review_note""",
        post_id, platform, title, body, tags, note,
    )  # fmt: skip


async def suppress_contact(contact_id: UUID, reason: str) -> None:
    """Add the address to the suppression list and drop anything still queued for this contact."""
    await db.execute(
        """insert into suppression_list (email_hash, reason)
           select email_hash(email), $2 from contacts where id=$1 and email is not null
           on conflict (email_hash) do nothing""",
        contact_id, reason,
    )  # fmt: skip
    await db.execute(
        "update emails set status='skipped', updated_at=now() where contact_id=$1 and status in ('drafted','approved')",
        contact_id,
    )


async def set_company_status(contact_id: UUID, status: str) -> None:
    """Never lifts a company out of suppressed."""
    await db.execute(
        """update companies set status=$2, updated_at=now()
            where id=(select company_id from contacts where id=$1) and status <> 'suppressed'""",
        contact_id, status,
    )  # fmt: skip


async def ensure_followup_step() -> None:
    """The daily-outreach campaign now has an intro and one follow-up."""
    await db.execute("update campaigns set steps=2 where name='daily-outreach' and steps < 2")


async def followup_candidates(delay_days: int, limit: int) -> list:
    """Intros sent at least `delay_days` ago with no open, no reply, no bounce and no follow-up yet."""
    cid = await campaign_id()
    return await db.fetch(
        """select e.id, e.contact_id, e.subject, e.body, e.message_id, c.name, c.role,
                  co.name as company, co.project_summary, co.technologies
             from emails e join contacts c on c.id=e.contact_id join companies co on co.id=c.company_id
            where e.campaign_id=$1 and e.step=1 and e.status='sent'
              and e.sent_at < now() - make_interval(days => $2)
              and e.opened_at is null and e.bounced_at is null
              and c.email is not null and not is_suppressed(c.email)
              and co.status not in ('suppressed','closed','rejected')
              and not exists (select 1 from replies r where r.contact_id=e.contact_id)
              and not exists (select 1 from emails f where f.contact_id=e.contact_id
                                 and f.campaign_id=e.campaign_id and f.step=2)
            order by e.sent_at limit $3""",
        cid, delay_days, limit,
    )  # fmt: skip


async def save_followup_draft(
    contact_id: UUID, subject: str, body: str, note: str | None, in_reply_to: str | None
) -> UUID | None:
    cid = await campaign_id()
    row = await db.fetchrow(
        """insert into emails (contact_id, campaign_id, step, status, subject, body, review_note,
                               in_reply_to, idempotency_key)
           values ($1,$2,2,'drafted',$3,$4,$5,$6,$7)
           on conflict (contact_id, campaign_id, step) do nothing returning id""",
        contact_id, cid, subject, body, note, in_reply_to, f"followup-{contact_id}",
    )  # fmt: skip
    return row["id"] if row else None
