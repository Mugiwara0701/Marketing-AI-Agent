from fastapi.testclient import TestClient

from app.main import app


def test_health():
    r = TestClient(app).get("/health").json()
    assert r["service"] == "outreach-service" and r["jobs"]
