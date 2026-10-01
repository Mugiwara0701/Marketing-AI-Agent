"""Database reads/writes for the daily run (thin wrappers over agentkit.db)."""

from datetime import date
from uuid import UUID

from agentkit import db


async def signal_seen(content_hash: str) -> bool:
    return (
        await db.fetchrow("select 1 from lead_signals where content_hash=$1", content_hash)
        is not None
    )


async def domain_known(domain: str) -> bool:
    return await db.fetchrow("select 1 from companies where domain=$1", domain) is not None


async def is_suppressed(addr: str) -> bool:
    row = await db.fetchrow("select is_suppressed($1) as s", addr)
    return bool(row and row["s"])


async def new_leads_today() -> int:
    """Actionable leads created today (a company with a contact and a drafted email)."""
    row = await db.fetchrow(
        "select count(*) as n from companies where created_at::date = current_date and status in ('contact_found','engaged')"
    )
    return int(row["n"]) if row else 0


async def record_signal(sig, extracted: dict, company_id: UUID | None = None) -> None:
    await db.execute(
        """insert into lead_signals (company_id, kind, source_url, raw_text, extracted, content_hash)
           values ($1,$2,$3,$4,$5::jsonb,$6) on conflict (content_hash) do nothing""",
        company_id, sig.kind, sig.url, sig.text[:6000], db.dumps(extracted), sig.hash,
    )  # fmt: skip


async def save_company(
    *, name: str, domain: str, status: str, q, source: str, source_url: str, review: bool
) -> UUID:
    row = await db.fetchrow(
        """insert into companies (name, domain, status, fit_score, fit_reason, source, source_url,
               needs_review, website, location, technologies, project_summary)
           values ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
           on conflict (domain) do update set updated_at = now()
           returning id""",
        name, domain, status, q.confidence, q.reason, source, source_url, review,
        f"https://{domain}", q.location or None, q.technologies, q.project_summary,
    )  # fmt: skip
    assert row is not None  # noqa: S101 - insert ... returning always yields a row
    return row["id"]


async def save_contact(company_id: UUID, c, source_url: str) -> UUID:
    row = await db.fetchrow(
        """insert into contacts (company_id, name, role, email, source_url, verification)
           values ($1,$2,$3,$4,$5,'unverified')
           on conflict (company_id, email) do update set collected_at = now()
           returning id""",
        company_id, c.name or None, c.role or None, c.email.lower(), source_url,
    )  # fmt: skip
    assert row is not None  # noqa: S101
    return row["id"]


async def campaign_id() -> UUID:
    row = await db.fetchrow("select id from campaigns where name='daily-outreach'")
    if row:
        return row["id"]
    row = await db.fetchrow(
        "insert into campaigns (name, steps) values ('daily-outreach', 1) returning id"
    )
    assert row is not None  # noqa: S101
    return row["id"]


async def save_email_draft(
    contact_id: UUID, subject: str, body: str, note: str | None
) -> UUID | None:
    cid = await campaign_id()
    row = await db.fetchrow(
        """insert into emails (contact_id, campaign_id, step, status, subject, body, review_note, idempotency_key)
           values ($1,$2,1,'drafted',$3,$4,$5,$6)
           on conflict (contact_id, campaign_id, step) do nothing returning id""",
        contact_id, cid, subject, body, note, f"intro-{contact_id}",
    )  # fmt: skip
    return row["id"] if row else None


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


async def companies_without_contact(limit: int = 15) -> list:
    """Qualified companies where no public contact was found yet (retried when lookup logic improves)."""
    return await db.fetch(
        "select id, name, domain, location, technologies, project_summary, source, source_url "
        "from companies where status='qualified' order by updated_at limit $1",
        limit,
    )


async def mark_contact_found(company_id: UUID) -> None:
    await db.execute(
        "update companies set status='contact_found', updated_at=now() where id=$1", company_id
    )


async def touch_company(company_id: UUID) -> None:
    await db.execute("update companies set updated_at=now() where id=$1", company_id)


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
