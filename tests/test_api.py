import time

from fastapi.testclient import TestClient

import main
from upi_link.auth import AdminAuth, LoginRateLimiter
from upi_link.cdk import CdkStore
from upi_link.jobs import JobManager


client = TestClient(main.app)


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
            "cdk": "UPI-TEST-TEST-TEST",
            "credential": "x" * 80,
            "email": "owner@example.com",
            "authorized": False,
        },
    )
    assert response.status_code == 400
    assert "授权" in response.json()["detail"]


def test_admin_login_and_cdk_generation(monkeypatch, tmp_path) -> None:
    store = CdkStore(tmp_path / "admin.db")
    monkeypatch.setattr(main, "cdks", store)
    monkeypatch.setattr(main, "admin_auth", AdminAuth("correct-password", "test-secret"))
    monkeypatch.setattr(main, "login_limiter", LoginRateLimiter())

    with TestClient(main.app) as admin:
        assert admin.get("/api/admin/cdks").status_code == 401
        assert admin.post("/api/admin/login", json={"password": "wrong"}).status_code == 401
        assert admin.post("/api/admin/login", json={"password": "correct-password"}).status_code == 200
        generated = admin.post(
            "/api/admin/cdks",
            json={
                "count": 2,
                "max_uses": 3,
                "expires_in_days": 30,
                "prefix": "TEST",
                "note": "private admin note",
            },
        )
        assert generated.status_code == 200
        items = generated.json()["items"]
        assert len(items) == 2

        verified = admin.post("/api/cdk/verify", json={"code": items[0]["code"]})
        assert verified.status_code == 200
        assert verified.json()["remaining_uses"] == 3
        assert "note" not in verified.json()
        assert "created_at" not in verified.json()


def test_batch_jobs_use_cdk_and_isolate_browser_sessions(monkeypatch, tmp_path) -> None:
    store = CdkStore(tmp_path / "batch.db")
    code = store.generate(count=1, max_uses=2, expires_in_days=30)[0]["code"]

    async def fake_runner(credential, options, qr_path, log, should_cancel):
        del options, should_cancel
        log(f"processing {credential.email}")
        qr_path.write_bytes(b"fake-png")
        return {
            "ok": True,
            "payment_link": f"https://payments.stripe.com/upi/instructions/{credential.email}",
            "qr_path": str(qr_path),
        }

    monkeypatch.setattr(main, "cdks", store)
    monkeypatch.setattr(main, "jobs", JobManager(tmp_path / "qr", max_concurrency=2, runner=fake_runner))

    with TestClient(main.app) as owner, TestClient(main.app) as stranger:
        owner.get("/")
        stranger.get("/")
        response = owner.post(
            "/api/jobs/batch",
            json={
                "cdk": code,
                "items": [
                    {"credential": "x" * 80, "email": "one@example.com"},
                    {"credential": "y" * 80, "email": "two@example.com"},
                ],
                "authorized": True,
            },
        )
        assert response.status_code == 202
        assert response.json()["count"] == 2

        owner_jobs = []
        for _ in range(100):
            owner_jobs = owner.get("/api/jobs").json()["jobs"]
            if len(owner_jobs) == 2 and all(job["status"] == "success" for job in owner_jobs):
                break
            time.sleep(0.01)

        assert len(owner_jobs) == 2
        assert all(job["status"] == "success" for job in owner_jobs)
        assert stranger.get("/api/jobs").json()["jobs"] == []
        assert store.verify(code)["used_uses"] == 2
