from fastapi.testclient import TestClient

from main import app


client = TestClient(app)


def test_health_and_index() -> None:
    health = client.get("/api/health")
    assert health.status_code == 200
    assert health.json()["ok"] is True

    index = client.get("/")
    assert index.status_code == 200
    assert "UPI 提链工具" in index.text


def test_job_creation_requires_authorization_confirmation() -> None:
    response = client.post(
        "/api/jobs",
        json={
            "credential": "x" * 80,
            "email": "owner@example.com",
            "authorized": False,
        },
    )
    assert response.status_code == 400
    assert "授权" in response.json()["detail"]

