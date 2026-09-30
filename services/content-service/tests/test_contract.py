from app.main import app
from fastapi.testclient import TestClient


def test_health():
    r = TestClient(app).get("/health").json()
    assert r["service"] == "content-service" and r["jobs"]
