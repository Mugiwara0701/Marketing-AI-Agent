"""Pipeline control REST API: token, start/stop requests, online/offline, CORS. The store is faked here; the real
Postgres round trip (with the agent's side) is in test_postgres.py."""

import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from api.app import create_app

NOW = datetime(2026, 10, 7, 10, 0, tzinfo=UTC)
AUTH = {"Authorization": "Bearer s3cret"}
BASE = "/api/v1/pipeline"


class FakeStore:
    def __init__(self, **row):
        self.row = {
            "desired_state": "stopped", "requested_by": None, "requested_at": None, "actual_state": "stopped",
            "state_since": None, "current_step": None, "current_pass_started_at": None, "next_pass_at": None,
            "last_pass": None, "sending_enabled": True, "test_mode": True, "agent_host": "office",
            "heartbeat_at": NOW - timedelta(seconds=5), **row,
        }  # fmt: skip
        self.requests: list[tuple[str, str]] = []

    async def read(self):
        return dict(self.row)

    async def request(self, state, by):
        self.requests.append((state, by))
        self.row |= {"desired_state": state, "requested_by": by, "requested_at": NOW}
        return dict(self.row)


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setenv("PIPELINE_API_TOKEN", "s3cret")
    monkeypatch.delenv("PIPELINE_API_CORS_ORIGINS", raising=False)


def call(store, method, path, **kw):
    async def go():
        transport = httpx.ASGITransport(app=create_app(store, now=lambda: NOW))
        async with httpx.AsyncClient(transport=transport, base_url="http://api") as c:
            return await c.request(method, path, **kw)

    return asyncio.run(go())


def test_token_is_required_but_not_for_health(monkeypatch):
    s = FakeStore()
    assert call(s, "GET", f"{BASE}/status").status_code == 401
    assert (
        call(s, "POST", f"{BASE}/start", headers={"Authorization": "Bearer nope"}).status_code
        == 401
    )
    assert call(s, "GET", "/health").json() == {"status": "ok"}
    monkeypatch.delenv("PIPELINE_API_TOKEN")
    assert call(s, "GET", f"{BASE}/status", headers=AUTH).status_code == 503
    assert s.requests == []


def test_start_records_who_asked_and_waits_for_the_agent():
    s = FakeStore()
    r = call(s, "POST", f"{BASE}/start", headers=AUTH, json={"requested_by": "akshat@x.com"}).json()
    assert s.requests == [("running", "dashboard:akshat@x.com")]
    assert (r["changed"], r["desired_state"], r["state"], r["in_sync"]) == (
        True,
        "running",
        "stopped",
        False,
    )
    again = call(s, "POST", f"{BASE}/start", headers=AUTH).json()  # no body is fine too
    assert again["changed"] is False and s.requests[-1] == ("running", "dashboard")


def test_stop_and_requested_by_is_cleaned():
    s = FakeStore(desired_state="running", actual_state="running")
    r = call(
        s, "POST", f"{BASE}/stop", headers=AUTH, json={"requested_by": "<script>x</script>"}
    ).json()
    assert r["changed"] is True and r["desired_state"] == "stopped"
    assert s.requests == [("stopped", "dashboard:scriptxscript")]


def test_running_pass_and_offline():
    running = FakeStore(
        desired_state="running", actual_state="running", current_step="leads",
        current_pass_started_at=NOW - timedelta(minutes=2),
        last_pass={"outcome": "succeeded", "result": {"leads": {"emails_drafted": 3}}},
    )  # fmt: skip
    r = call(running, "GET", f"{BASE}/status", headers=AUTH).json()
    assert (r["state"], r["online"], r["in_sync"], r["current_pass"]["step"]) == (
        "running",
        True,
        True,
        "leads",
    )
    assert r["last_pass"]["result"]["leads"]["emails_drafted"] == 3
    gone = FakeStore(desired_state="running", actual_state="running", current_step="leads",
                     current_pass_started_at=NOW, heartbeat_at=NOW - timedelta(minutes=10))  # fmt: skip
    r = call(gone, "GET", f"{BASE}/status", headers=AUTH).json()
    assert (r["state"], r["online"], r["in_sync"], r["current_pass"]) == (
        "offline",
        False,
        False,
        None,
    )
    never = FakeStore(heartbeat_at=None)
    assert call(never, "GET", f"{BASE}/status", headers=AUTH).json()["state"] == "offline"


def test_methods_validation_and_cors(monkeypatch):
    s = FakeStore()
    assert call(s, "GET", f"{BASE}/start", headers=AUTH).status_code == 405
    too_long = {"requested_by": "x" * 101}
    assert call(s, "POST", f"{BASE}/start", headers=AUTH, json=too_long).status_code == 422
    monkeypatch.setenv("PIPELINE_API_CORS_ORIGINS", "https://dash.example.com")
    pre = {"Origin": "https://dash.example.com", "Access-Control-Request-Method": "POST"}
    r = call(s, "OPTIONS", f"{BASE}/start", headers=pre)
    assert r.headers.get("access-control-allow-origin") == "https://dash.example.com"
    bad = call(s, "OPTIONS", f"{BASE}/start", headers=pre | {"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in bad.headers


def test_openapi_documents_the_endpoints():
    paths = call(FakeStore(), "GET", "/openapi.json").json()["paths"]
    assert {f"{BASE}/status", f"{BASE}/start", f"{BASE}/stop", "/health"} <= set(paths)
