"""Postgres access (Supabase, pooled connection string). Services use the service_role/DB URL only."""

import hashlib
import json
from typing import Any
from urllib.parse import unquote

import asyncpg

from .config import env

_pool: asyncpg.Pool | None = None


def parse_dsn(url: str) -> dict[str, Any]:
    """Split postgresql://user:password@host:port/db into connect kwargs.

    Splits on the LAST '@' and the FIRST ':' so a password containing '@', '?', '#' or '/' works
    without URL-encoding (Supabase generates such passwords). Query options are ignored.
    """
    rest = url.split("://", 1)[-1]
    creds, _, hostpart = rest.rpartition("@")
    user, _, password = creds.partition(":")
    hostport, _, dbname = hostpart.partition("/")
    host, _, port = hostport.partition(":")
    return {
        "host": host,
        "port": int(port or 5432),
        "user": unquote(user),
        "password": unquote(password),
        "database": dbname.split("?", 1)[0] or "postgres",
    }


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        # statement_cache_size=0: required behind pgbouncer/Supabase transaction pooler.
        _pool = await asyncpg.create_pool(
            **parse_dsn(env("DATABASE_URL", required=True)),
            min_size=1,
            max_size=5,
            statement_cache_size=0,
        )
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


async def fetch(sql: str, *args: Any) -> list[asyncpg.Record]:
    return await (await get_pool()).fetch(sql, *args)


async def fetchrow(sql: str, *args: Any) -> asyncpg.Record | None:
    return await (await get_pool()).fetchrow(sql, *args)


async def execute(sql: str, *args: Any) -> str:
    return await (await get_pool()).execute(sql, *args)


def vec(v: list[float]) -> str:
    """pgvector text literal; use with a `$n::vector` cast."""
    return "[" + ",".join(f"{x:.7g}" for x in v) + "]"


def email_hash(addr: str) -> str:
    """Matches the SQL function email_hash()."""
    return hashlib.sha256(addr.strip().lower().encode()).hexdigest()


async def claim(
    table: str, from_status: str, to_status: str, limit: int = 20
) -> list[asyncpg.Record]:
    """Atomically move up to `limit` rows from one status to the next (FOR UPDATE SKIP LOCKED).

    Late, dropped or duplicate runs cannot double-process a row. `table` must be a trusted literal.
    """
    if not table.isidentifier():
        raise ValueError("bad table name")
    sql = f"""
        update {table} set status = $2
         where id in (select id from {table} where status = $1
                       order by created_at limit $3 for update skip locked)
        returning *"""
    return await fetch(sql, from_status, to_status, limit)


async def start_run(service: str, job: str, run_id: str) -> None:
    await execute(
        "insert into agent_runs (service, job, run_id, status) values ($1,$2,$3,'running')",
        service,
        job,
        run_id,
    )


async def finish_run(
    run_id: str, status: str, result: dict | None = None, error: str | None = None
) -> None:
    r = result or {}
    await execute(
        """update agent_runs set status=$2, finished_at=now(), error=$3,
               items_processed=$4, items_failed=$5, tokens_in=$6, tokens_out=$7
           where run_id=$1 and finished_at is null""",
        run_id,
        status,
        error,
        int(r.get("processed", 0)),
        int(r.get("failed", 0)),
        int(r.get("tokens_in", 0)),
        int(r.get("tokens_out", 0)),
    )


async def log_retrieval(
    task: str,
    prompt_version: int | None,
    example_ids: list,
    chunk_ids: list,
    outcome: str | None = None,
) -> None:
    await execute(
        "insert into retrieval_logs (task, prompt_version, example_ids, chunk_ids, outcome) "
        "values ($1,$2,$3,$4,$5)",
        task,
        prompt_version,
        example_ids,
        chunk_ids,
        outcome,
    )


def dumps(obj: Any) -> str:
    return json.dumps(obj, default=str)
