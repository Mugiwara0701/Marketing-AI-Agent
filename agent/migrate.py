"""Apply supabase/migrations/*.sql in order, once each (tracked in schema_migrations)."""

from pathlib import Path

from agentkit import db


async def apply() -> list[str]:
    await db.execute(
        "create table if not exists schema_migrations (name text primary key, applied_at timestamptz default now())"
    )
    done = {r["name"] for r in await db.fetch("select name from schema_migrations")}
    applied = []
    for f in sorted(Path("supabase/migrations").glob("*.sql")):  # noqa: ASYNC240 - one-shot CLI
        if f.name in done:
            continue
        pool = await db.get_pool()
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute(f.read_text(encoding="utf-8"))
            await conn.execute("insert into schema_migrations (name) values ($1)", f.name)
        applied.append(f.name)
    return applied
