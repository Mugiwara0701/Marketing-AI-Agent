import time

import jwt
import pytest
from app.main import app
from fastapi.testclient import TestClient

SECRET = "test-secret-that-is-long-enough-for-hs256"  # noqa: S105
client = TestClient(app)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_JWT_SECRET", SECRET)


def _token(**over):
    claims = {
        "sub": "u1",
        "email": "a@b.co",
        "role": "authenticated",
        "aud": "authenticated",
        "exp": int(time.time()) + 60,
        **over,
    }
    return jwt.encode(claims, SECRET, algorithm="HS256")


def _get(token=None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return client.get("/auth/me", headers=headers)


def test_health_is_public():
    assert client.get("/health").json() == {"ok": True}


def test_valid_token():
    r = _get(_token())
    assert r.status_code == 200
    assert r.json() == {"id": "u1", "email": "a@b.co", "role": "authenticated"}


def test_missing_token():
    assert _get().status_code == 401


def test_expired_token():
    assert _get(_token(exp=int(time.time()) - 10)).status_code == 401


def test_wrong_audience():
    assert _get(_token(aud="other")).status_code == 401


def test_bad_signature():
    bad = jwt.encode(
        {"sub": "u1", "aud": "authenticated", "exp": int(time.time()) + 60},
        "x" * 40,
        algorithm="HS256",
    )
    assert _get(bad).status_code == 401
