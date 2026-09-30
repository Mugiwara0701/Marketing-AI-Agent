"""The job HTTP contract shared by all three services.

    GET  /health          -> service status and registered jobs
    POST /jobs/{name}     -> 202 {"run_id": ...}; runs in the background
    GET  /runs/{run_id}   -> {"status": "running|succeeded|failed", "result"|"error": ...}

Auth: header `X-Job-Token` must equal env JOB_TOKEN (checked at request time).
Only one run of a given job at a time (409 otherwise).
"""

import asyncio
import hmac
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from .log import get_logger

MAX_RUNS = 200


@dataclass
class JobContext:
    run_id: str
    job: str
    payload: dict = field(default_factory=dict)


JobFn = Callable[[JobContext], Awaitable[dict | None]]


class JobRequest(BaseModel):
    payload: dict = Field(default_factory=dict)


class RunStatus(BaseModel):
    run_id: str
    job: str
    status: str
    started_at: float
    finished_at: float | None = None
    result: dict | None = None
    error: str | None = None


def _check_token(x_job_token: str | None) -> None:
    expected = os.environ.get("JOB_TOKEN", "")
    if not expected:
        raise HTTPException(status_code=503, detail="JOB_TOKEN not configured")
    if not x_job_token or not hmac.compare_digest(x_job_token.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="invalid token")


def create_app(service: str, jobs: dict[str, JobFn]) -> FastAPI:
    app = FastAPI(title=service)
    log = get_logger(service)
    runs: dict[str, RunStatus] = {}
    active: set[str] = set()
    tasks: set[asyncio.Task] = set()

    async def _run(run_id: str, name: str, payload: dict) -> None:
        try:
            result = await jobs[name](JobContext(run_id=run_id, job=name, payload=payload))
            runs[run_id].status = "succeeded"
            runs[run_id].result = result or {}
        except NotImplementedError:
            runs[run_id].status = "failed"
            runs[run_id].error = "not implemented"
        except Exception as exc:  # noqa: BLE001 - report every failure to the caller
            log.exception("job failed", extra={"ctx": {"job": name, "run_id": run_id}})
            runs[run_id].status = "failed"
            runs[run_id].error = f"{type(exc).__name__}: {exc}"
        finally:
            runs[run_id].finished_at = time.time()
            active.discard(name)

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"service": service, "status": "ok", "jobs": sorted(jobs), "running": sorted(active)}

    @app.post("/jobs/{name}", status_code=202)
    async def start(
        name: str,
        body: JobRequest | None = None,
        x_job_token: str | None = Header(default=None),
    ) -> dict[str, str]:
        _check_token(x_job_token)
        if name not in jobs:
            raise HTTPException(status_code=404, detail="unknown job")
        if name in active:
            raise HTTPException(status_code=409, detail="job already running")
        run_id = uuid.uuid4().hex
        active.add(name)
        runs[run_id] = RunStatus(run_id=run_id, job=name, status="running", started_at=time.time())
        while len(runs) > MAX_RUNS:
            runs.pop(next(iter(runs)))
        task = asyncio.create_task(_run(run_id, name, (body.payload if body else {})))
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return {"run_id": run_id}

    @app.get("/runs/{run_id}")
    async def get_run(run_id: str, x_job_token: str | None = Header(default=None)) -> RunStatus:
        _check_token(x_job_token)
        if run_id not in runs:
            raise HTTPException(status_code=404, detail="unknown run")
        return runs[run_id]

    return app
