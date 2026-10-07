"""Pipeline control REST API: what the dashboard backend calls to start and stop the agent.

The agent runs on the office machine, behind NAT; nothing can call it. This API (hosted anywhere: next to the
dashboard backend, a VPS, Render...) writes the requested state into the database, and the agent polls it every 2 s,
applies it and reports back with a heartbeat (agent/control.py).

    GET  /health                    no token: the API is up (says nothing about the office machine)
    GET  /api/v1/pipeline/status    what the agent is doing (state, step, last pass, online)
    POST /api/v1/pipeline/start     ask the agent to start (applied within ~2 s)
    POST /api/v1/pipeline/stop      ask the agent to stop (the pass in progress is cancelled)

Auth: `Authorization: Bearer <PIPELINE_API_TOKEN>`. Interactive docs: /docs (OpenAPI: /openapi.json).
Run: `uvicorn api.app:app --host 0.0.0.0 --port 8000`. Settings: see api/README.md.
"""

import hmac
import os
import re
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from .store import PostgresStore, State, Store


class CurrentPass(BaseModel):
    started_at: datetime
    step: str | None = Field(
        description="inbox | replies | send | followups | leads | blog (null for a moment while a step begins)"
    )


class PipelineStatus(BaseModel):
    desired_state: State = Field(description="What the dashboard last asked for.")
    state: Literal["running", "stopped", "offline"] = Field(
        description="What the agent is doing; offline when its heartbeat is too old (machine off, no network, "
        "service stopped)."
    )
    online: bool
    in_sync: bool = Field(description="True once the agent has applied desired_state.")
    requested_by: str | None
    requested_at: datetime | None
    state_since: datetime | None = Field(
        description="When the agent last started or stopped (null when offline)."
    )
    current_pass: CurrentPass | None = Field(
        description="The pass in progress (null between passes or offline)."
    )
    next_pass_at: datetime | None = Field(
        description="While running, between two passes: when the next starts."
    )
    last_pass: dict[str, Any] | None = Field(
        description="The last finished pass: started_at, finished_at, outcome (succeeded | failed | stopped), "
        "result (numbers per step; a failed step has an `error` key) or error."
    )
    sending_enabled: bool | None = Field(description="False: approved emails are not sent at all.")
    test_mode: bool | None = Field(
        description="True: every email goes to the test inbox, not the real contact."
    )
    agent_host: str | None
    heartbeat_at: datetime | None


class ChangeResult(PipelineStatus):
    changed: bool = Field(description="False when the dashboard had already asked for this state.")


class ChangeRequest(BaseModel):
    requested_by: str | None = Field(
        default=None,
        max_length=100,
        description="Who pressed the button (stored with the request).",
    )


def view(row: dict[str, Any], now: datetime, offline_after_s: float) -> PipelineStatus:
    beat: datetime | None = row.get("heartbeat_at")
    online = beat is not None and (now - beat).total_seconds() <= offline_after_s
    started = row.get("current_pass_started_at")
    return PipelineStatus(
        desired_state=row["desired_state"],
        state=row["actual_state"] if online else "offline",
        online=online,
        in_sync=online and row["desired_state"] == row["actual_state"],
        requested_by=row.get("requested_by"),
        requested_at=row.get("requested_at"),
        state_since=row.get("state_since") if online else None,
        current_pass=CurrentPass(started_at=started, step=row.get("current_step"))
        if online and started
        else None,
        next_pass_at=row.get("next_pass_at") if online else None,
        last_pass=row.get("last_pass"),
        sending_enabled=row.get("sending_enabled"),
        test_mode=row.get("test_mode"),
        agent_host=row.get("agent_host"),
        heartbeat_at=beat,
    )


_bearer = HTTPBearer(auto_error=False)


def _require_token(
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> None:
    expected = os.environ.get("PIPELINE_API_TOKEN", "")
    if not expected:
        raise HTTPException(503, "PIPELINE_API_TOKEN not configured")
    if creds is None or not hmac.compare_digest(creds.credentials.encode(), expected.encode()):
        raise HTTPException(401, "invalid token", headers={"WWW-Authenticate": "Bearer"})


def create_app(
    store: Store | None = None, now: Callable[[], datetime] = lambda: datetime.now(UTC)
) -> FastAPI:
    """`store` is for tests; without it the app opens a Postgres pool on DATABASE_URL at startup."""
    offline_after = float(os.environ.get("PIPELINE_OFFLINE_AFTER_SECONDS", "60"))

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if store is not None:  # tests: attached below, nothing to open
            yield
            return
        pg = PostgresStore(os.environ["DATABASE_URL"])
        await pg.open()
        app.state.store = pg
        try:
            yield
        finally:
            await pg.close()

    docs = os.environ.get("PIPELINE_API_DOCS", "true").lower() != "false"
    app = FastAPI(
        title="Marketing agent: pipeline control",
        version="1.0.0",
        description="Start / stop the lead + outreach pipeline running on the office machine. "
        "Requests are applied by the agent within ~2 s; poll `status` until `in_sync`.",
        lifespan=lifespan,
        docs_url="/docs" if docs else None,
        redoc_url=None,
        openapi_url="/openapi.json" if docs else None,
    )
    if store is not None:
        app.state.store = store
    origins = [
        o.strip() for o in os.environ.get("PIPELINE_API_CORS_ORIGINS", "").split(",") if o.strip()
    ]
    if origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_methods=["GET", "POST"],
            allow_headers=["Authorization", "Content-Type"],
        )

    def _store(request: Request) -> Store:
        return request.app.state.store

    async def _change(want: State, body: ChangeRequest | None, s: Store) -> ChangeResult:
        who = re.sub(r"[^\w.@ +-]", "", (body.requested_by if body else None) or "")[:100]
        before = await s.read()
        row = await s.request(want, f"dashboard:{who}" if who else "dashboard")
        if row is None or before is None:
            raise HTTPException(500, "pipeline_control missing: run the agent's migrations")
        status = view(row, now(), offline_after)
        return ChangeResult(changed=before["desired_state"] != want, **status.model_dump())

    @app.get("/health", tags=["health"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    auth = [Depends(_require_token)]

    @app.get("/api/v1/pipeline/status", dependencies=auth, tags=["pipeline"])
    async def status(s: Annotated[Store, Depends(_store)]) -> PipelineStatus:
        row = await s.read()
        if row is None:
            raise HTTPException(500, "pipeline_control missing: run the agent's migrations")
        return view(row, now(), offline_after)

    @app.post("/api/v1/pipeline/start", dependencies=auth, tags=["pipeline"])
    async def start(
        s: Annotated[Store, Depends(_store)], body: ChangeRequest | None = None
    ) -> ChangeResult:
        return await _change("running", body, s)

    @app.post("/api/v1/pipeline/stop", dependencies=auth, tags=["pipeline"])
    async def stop(
        s: Annotated[Store, Depends(_store)], body: ChangeRequest | None = None
    ) -> ChangeResult:
        return await _change("stopped", body, s)

    return app


def __getattr__(name: str) -> FastAPI:
    # `uvicorn api.app:app`: built on first access, so importing this module (tests) needs no DATABASE_URL.
    if name == "app":
        return create_app()
    raise AttributeError(name)
