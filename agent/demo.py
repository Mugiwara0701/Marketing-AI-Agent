"""One command that checks the setup, runs the whole daily pipeline for real, and shows the results."""

import httpx
from pydantic import BaseModel

from agentkit import db, llm
from agentkit.config import env

from . import mailer, migrate
from . import run as runner


class _Ping(BaseModel):
    word: str


def _line(ok: bool | None, name: str, detail: str) -> None:
    mark = {True: "OK  ", False: "FAIL", None: "skip"}[ok]
    print(f"[{mark}] {name}: {detail}")  # noqa: T201


async def _check_llm() -> bool:
    if not env("LLM_BASE_URL") or not await llm.health():
        _line(
            False, "LLM host", f"{env('LLM_BASE_URL') or 'LLM_BASE_URL not set'} is not reachable"
        )
        return False
    _line(True, "LLM host", env("LLM_BASE_URL") or "")
    try:
        msg = [{"role": "user", "content": 'Reply with JSON {"word":"ping"}'}]
        c = await llm.complete("lead.assess", msg, _Ping, max_tokens=30, reasoning_effort="none")
    except Exception as exc:
        _line(False, "LLM structured reply", type(exc).__name__)
        return False
    _line(True, "LLM structured reply", f"model {c.model}")
    return True


async def _check_db() -> bool:
    if not env("DATABASE_URL"):
        _line(False, "Database", "DATABASE_URL not set")
        return False
    try:
        await db.fetchrow("select 1")
    except Exception as exc:
        _line(
            False,
            "Database",
            f"{type(exc).__name__}: check DATABASE_URL (use the Supabase pooler string)",
        )
        return False
    _line(True, "Database", "connected")
    return True


async def _check_optional() -> None:
    searx = env("SEARXNG_URL")
    if searx:
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get(
                    f"{searx.rstrip('/')}/search", params={"q": "aosp", "format": "json"}
                )
            _line(r.status_code == 200, "Web search (SearXNG)", searx)
        except httpx.HTTPError:
            _line(False, "Web search (SearXNG)", f"{searx} unreachable (job feeds still work)")
    else:
        _line(None, "Web search", "SEARXNG_URL not set: only job feeds are searched")

    token = env("SLACK_BOT_TOKEN")
    if token:
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.post(
                    "https://slack.com/api/auth.test", headers={"Authorization": f"Bearer {token}"}
                )
            data = r.json()
            _line(
                bool(data.get("ok")),
                "Slack bot token",
                "valid" if data.get("ok") else str(data.get("error")),
            )
        except httpx.HTTPError:
            _line(False, "Slack bot token", "slack.com unreachable")
    else:
        _line(None, "Slack", "SLACK_BOT_TOKEN not set: nothing is posted to Slack")
    on = mailer.sending_enabled()
    _line(True if on else None, "Email sending", "ENABLED" if on else "off (drafts only)")


async def preflight() -> bool:
    """Prints a checklist. Returns False only if something REQUIRED is broken."""
    llm_ok = await _check_llm()
    db_ok = await _check_db()
    await _check_optional()
    return llm_ok and db_ok


async def summary() -> None:
    print("\n================ RESULTS (saved in the database) ================")  # noqa: T201
    row = await db.fetchrow(
        """select (select count(*) from companies where created_at::date=current_date) companies,
                  (select count(*) from companies where created_at::date=current_date and status='contact_found') with_contact,
                  (select count(*) from emails where created_at::date=current_date) drafts,
                  (select count(*) from content_posts where created_at::date=current_date) posts"""
    )
    if row:
        print(  # noqa: T201
            f"Today: {row['companies']} companies, {row['with_contact']} with a public contact, "
            f"{row['drafts']} proposal drafts, {row['posts']} blog post(s)"
        )
    leads = await db.fetch(
        """select e.id, co.name, co.domain, co.location, co.technologies, c.email, e.subject, e.status
             from emails e join contacts c on c.id=e.contact_id join companies co on co.id=c.company_id
            where e.created_at::date=current_date order by e.created_at"""
    )
    for r in leads:
        print(  # noqa: T201
            f"\n LEAD  {r['name']} ({r['domain']}) {r['location'] or ''}  tech={r['technologies']}\n"
            f"       to: {r['email']}  [{r['status']}]\n       subject: {r['subject']}\n       id: {r['id']}"
        )
    posts = await db.fetch(
        "select title, status, tags, length(body_md) n from content_posts where created_at::date=current_date"
    )
    for r in posts:
        print(f"\n BLOG  {r['title']}  [{r['status']}]  tags={r['tags']}  {r['n']} chars")  # noqa: T201
    print("\nRead a draft: python -m agent show <id>    List drafts: python -m agent review")  # noqa: T201


async def run(execute: bool = True) -> int:
    if not await preflight():
        print("\nFix the FAIL lines above, then run this command again.")  # noqa: T201
        return 1
    print("\napplied migrations:", await migrate.apply() or "none needed")  # noqa: T201
    if execute:
        print("\nRunning the full daily pipeline (leads, then blog). This takes several minutes...")  # noqa: T201
        print("run result:", await runner.daily_run(force=True))  # noqa: T201
    await summary()
    return 0
