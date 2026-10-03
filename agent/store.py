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
    *,
    name: str,
    domain: str,
    status: str,
    q,
    source: str,
    source_url: str,
    review: bool,
    form_url: str | None = None,
    manual_reason: str | None = None,
) -> UUID:
    row = await db.fetchrow(
        """insert into companies (name, domain, status, fit_score, fit_reason, source, source_url,
               needs_review, website, location, technologies, project_summary, contact_form_url, manual_reason)
           values ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)
           on conflict (domain) do update set updated_at = now(),
               contact_form_url = coalesce(excluded.contact_form_url, companies.contact_form_url),
               manual_reason = coalesce(excluded.manual_reason, companies.manual_reason)
           returning id""",
        name, domain, status, q.confidence, q.reason, source, source_url, review,
        f"https://{domain}", q.location or None, q.technologies, q.project_summary, form_url, manual_reason,
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


async def set_form_url(company_id: UUID, form_url: str) -> None:
    await db.execute(
        "update companies set contact_form_url=$2, updated_at=now() where id=$1",
        company_id,
        form_url,
    )


async def company_with_form(ref: str):
    """A company that has a contact form, by domain or id."""
    return await db.fetchrow(
        "select id, name, domain, location, technologies, project_summary, contact_form_url "
        "from companies where contact_form_url is not null and (domain=$1 or id::text=$1)",
        ref,
    )


async def set_manual_reason(company_id: UUID, reason: str) -> None:
    await db.execute(
        "update companies set manual_reason=$2, updated_at=now() where id=$1", company_id, reason
    )


async def companies_to_contact_manually() -> list:
    """Companies we could not read automatically: a bot check on their site, or only a contact form."""
    return await db.fetch(
        "select name, domain, manual_reason, contact_form_url, project_summary from companies "
        "where (manual_reason is not null or contact_form_url is not null) and status <> 'contact_found' "
        "order by created_at desc"
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
