"""The pipeline_control row (supabase/migrations/0011_pipeline_control.sql), read and written with asyncpg.

The API only records what the dashboard wants (request_pipeline()) and reads what the agent reported; it never talks
to the office machine."""

import json
from typing import Any, Literal, Protocol

import asyncpg

State = Literal["running", "stopped"]


class Store(Protocol):
    async def read(self) -> dict[str, Any] | None: ...
    async def request(self, state: State, by: str) -> dict[str, Any] | None: ...


def _row(r: asyncpg.Record | None) -> dict[str, Any] | None:
    if r is None:
        return None
    d = dict(r)
    if isinstance(d.get("last_pass"), str):  # jsonb comes back as text without a codec
        d["last_pass"] = json.loads(d["last_pass"])
    return d


class PostgresStore:
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self.pool: asyncpg.Pool | None = None

    async def open(self) -> None:
        # statement_cache_size=0: required behind the Supabase transaction pooler (pgbouncer).
        self.pool = await asyncpg.create_pool(
            self.dsn, min_size=1, max_size=4, statement_cache_size=0
        )

    async def close(self) -> None:
        if self.pool:
            await self.pool.close()
            self.pool = None

    def _pool(self) -> asyncpg.Pool:
        if self.pool is None:
            raise RuntimeError("store not opened")
        return self.pool

    async def read(self) -> dict[str, Any] | None:
        return _row(await self._pool().fetchrow("select * from pipeline_control where id = 1"))

    async def request(self, state: State, by: str) -> dict[str, Any] | None:
        return _row(
            await self._pool().fetchrow("select * from request_pipeline($1, $2)", state, by)
        )
