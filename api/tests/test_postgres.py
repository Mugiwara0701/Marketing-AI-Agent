"""The whole control loop on a real Postgres: the API records a request, the agent applies it and reports back,
the API shows it. Needs a throwaway database (never production), same as agent/tests/test_leadgen_postgres.py:
TEST_DATABASE_URL=postgresql://postgres:test@127.0.0.1:55432/postgres  (skipped when not set)."""

import asyncio
import os
from pathlib import Path

import asyncpg
import httpx
import pytest

from agent import control, run
from agentkit import db
from api.app import create_app

URL = os.environ.get("TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not URL, reason="set TEST_DATABASE_URL to a throwaway Postgres")


def test_dashboard_start_reaches_the_agent_and_back(monkeypatch):
    monkeypatch.setenv("PIPELINE_API_TOKEN", "s3cret")
    monkeypatch.setenv("DATABASE_URL", URL)
    migration = Path("supabase/migrations/0011_pipeline_control.sql").read_text()

    async def slow_run(*, force, on_step):
        on_step("leads")
        await asyncio.sleep(60)

    monkeypatch.setattr(run, "daily_run", slow_run)

    async def go():
        conn = await asyncpg.connect(URL)
        await conn.execute("drop table if exists pipeline_control cascade")
        await conn.execute(migration)
        await conn.close()
        auth = {"Authorization": "Bearer s3cret"}
        app = create_app()
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://api") as api,
        ):
            before = (await api.get("/api/v1/pipeline/status", headers=auth)).json()
            asked = (
                await api.post("/api/v1/pipeline/start", headers=auth, json={"requested_by": "qa"})
            ).json()
            pipeline, stop = control.Pipeline(rest_minutes=60), asyncio.Event()
            store = control.PostgresControl(host="office")
            agent = asyncio.create_task(control.link(pipeline, store, stop, 0.05))
            await asyncio.sleep(0.3)
            running = (await api.get("/api/v1/pipeline/status", headers=auth)).json()
            await api.post("/api/v1/pipeline/stop", headers=auth)
            await asyncio.sleep(0.3)
            stopped = (await api.get("/api/v1/pipeline/status", headers=auth)).json()
            stop.set()
            await agent
            await control.shutdown(pipeline, store)
            offline = (await api.get("/api/v1/pipeline/status", headers=auth)).json()
        await db.close_pool()
        return before, asked, running, stopped, offline

    before, asked, running, stopped, offline = asyncio.run(go())
    assert (
        before["state"] == "offline" and before["desired_state"] == "stopped"
    )  # fresh install, no agent yet
    assert asked["desired_state"] == "running" and asked["requested_by"] == "dashboard:qa"
    assert (
        running["state"] == "running"
        and running["in_sync"]
        and running["current_pass"]["step"] == "leads"
    )
    assert running["agent_host"] == "office"
    assert (
        stopped["state"] == "stopped"
        and stopped["in_sync"]
        and stopped["last_pass"]["outcome"] == "stopped"
    )
    assert offline["state"] == "offline" and offline["desired_state"] == "stopped"


def test_the_api_login_can_only_read_the_row_and_request(monkeypatch):
    """Migration 0012: the role the hosted API logs in with reads pipeline_control and calls request_pipeline(),
    nothing else; Supabase's anon / authenticated roles cannot start or stop the pipeline."""
    monkeypatch.setenv("DATABASE_URL", URL)
    migrations = [
        Path(f"supabase/migrations/{n}").read_text()
        for n in ("0011_pipeline_control.sql", "0012_pipeline_api_role.sql")
    ]

    async def go():
        admin = await asyncpg.connect(URL)
        await admin.execute("""
            drop table if exists pipeline_control cascade; drop table if exists secrets_demo;
            drop owned by pipeline_api; drop role if exists pipeline_api;
            do $$ begin create role anon nologin; exception when duplicate_object then null; end $$;
            create table secrets_demo (x text); insert into secrets_demo values ('lead data');
        """)
        for sql in migrations:
            await admin.execute(sql)
        api_url = await control.api_credentials(URL)
        await db.close_pool()
        await admin.close()

        api = await asyncpg.connect(api_url)
        row = await api.fetchrow("select * from request_pipeline('running', 'dashboard:qa')")
        seen = await api.fetchval("select desired_state from pipeline_control where id = 1")
        refused = []
        for sql in (
            "select * from secrets_demo",
            "update pipeline_control set desired_state = 'stopped'",
            "delete from pipeline_control",
        ):
            try:
                await api.execute(sql)
            except asyncpg.InsufficientPrivilegeError:
                refused.append(sql.split()[0])
        await api.close()

        admin = await asyncpg.connect(URL)
        anon_can = await admin.fetchval(
            "select has_function_privilege('anon', 'request_pipeline(text, text)', 'execute')"
        )
        await admin.close()
        return api_url, row["desired_state"], seen, refused, anon_can

    api_url, requested, seen, refused, anon_can = asyncio.run(go())
    assert api_url.startswith("postgresql://pipeline_api:")
    assert requested == "running" and seen == "running"
    assert refused == ["select", "update", "delete"]
    assert anon_can is False
