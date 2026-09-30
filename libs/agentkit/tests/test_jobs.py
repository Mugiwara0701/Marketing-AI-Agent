import asyncio

import pytest
from agentkit import JobContext, create_app
from fastapi.testclient import TestClient

H = {"X-Job-Token": "secret"}


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setenv("JOB_TOKEN", "secret")


async def ok(ctx: JobContext):
    return {"echo": ctx.payload}


async def boom(ctx: JobContext):
    raise ValueError("bad")


async def todo(ctx: JobContext):
    raise NotImplementedError


async def slow(ctx: JobContext):
    await asyncio.sleep(0.5)
    return {}


def client():
    return TestClient(create_app("t", {"ok": ok, "boom": boom, "todo": todo, "slow": slow}))


def wait(c, run_id, tries=50):
    import time

    for _ in range(tries):
        r = c.get(f"/runs/{run_id}", headers=H).json()
        if r["status"] != "running":
            return r
        time.sleep(0.05)
    raise AssertionError("timeout")


def test_health_lists_jobs():
    r = client().get("/health").json()
    assert r["status"] == "ok" and "ok" in r["jobs"]


def test_auth_required():
    c = client()
    assert c.post("/jobs/ok").status_code == 401
    assert c.post("/jobs/ok", headers={"X-Job-Token": "wrong"}).status_code == 401


def test_unknown_job():
    assert client().post("/jobs/nope", headers=H).status_code == 404


def test_success_and_payload():
    with client() as c:
        rid = c.post("/jobs/ok", headers=H, json={"payload": {"a": 1}}).json()["run_id"]
        r = wait(c, rid)
        assert r["status"] == "succeeded" and r["result"] == {"echo": {"a": 1}}


def test_failure_and_not_implemented():
    with client() as c:
        r = wait(c, c.post("/jobs/boom", headers=H).json()["run_id"])
        assert r["status"] == "failed" and "ValueError" in r["error"]
        r = wait(c, c.post("/jobs/todo", headers=H).json()["run_id"])
        assert r["error"] == "not implemented"


def test_conflict_while_running():
    with client() as c:
        rid = c.post("/jobs/slow", headers=H).json()["run_id"]
        assert c.post("/jobs/slow", headers=H).status_code == 409
        wait(c, rid)
