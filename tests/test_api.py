import time

from fastapi.testclient import TestClient

import main
from upi_link.auth import AdminAuth, LoginRateLimiter
from upi_link.cdk import CdkStore
from upi_link.jobs import JobManager
from upi_link.settings import SettingsStore


client = TestClient(main.app)


def test_health_and_index() -> None:
    health = client.get("/api/health")
    assert health.status_code == 200
    assert health.json()["ok"] is True
    assert health.json()["cdk_required"] is True
    assert "admin_enabled" not in health.json()
    assert "config" not in health.json()
    assert "max_concurrency" not in health.json()

    index = client.get("/")
    assert index.status_code == 200
    assert "UPI 提链工具" in index.text
    assert client.get("/docs").status_code == 404
    assert client.get("/redoc").status_code == 404
    assert client.get("/openapi.json").status_code == 404


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
    monkeypatch.setattr(main, "settings", SettingsStore(tmp_path / "admin.db"))
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

        saved = admin.put(
            "/api/admin/settings",
            json={
                "proxy_pool": "http://user:pass@proxy.example:2000\nhttp://user2:pass@proxy.example:2000",
                "login_proxy": "http://login:pass@login.example:2000",
                "approve_retries": 40,
                "approve_concurrency": 5,
                "proxy_from_step": 3,
            },
        )
        assert saved.status_code == 200
        assert len(saved.json()["proxy_pool"]) == 2
        assert admin.get("/api/admin/settings").json()["approve_concurrency"] == 5


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
    monkeypatch.setattr(main, "settings", SettingsStore(tmp_path / "batch.db"))
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


def test_failed_job_releases_cdk_for_reuse(monkeypatch, tmp_path) -> None:
    store = CdkStore(tmp_path / "failed-job.db")
    code = store.generate(count=1, max_uses=1, expires_in_days=30)[0]["code"]

    async def failed_runner(credential, options, qr_path, log, should_cancel):
        del credential, options, qr_path, log, should_cancel
        return {"ok": False, "error": "expected failure"}

    monkeypatch.setattr(main, "cdks", store)
    monkeypatch.setattr(main, "settings", SettingsStore(tmp_path / "failed-job.db"))
    monkeypatch.setattr(main, "jobs", JobManager(tmp_path / "failed-qr", runner=failed_runner))

    with TestClient(main.app) as owner:
        owner.get("/")
        payload = {
            "cdk": code,
            "credential": "x" * 80,
            "email": "owner@example.com",
            "authorized": True,
        }
        first = owner.post("/api/jobs", json=payload)
        assert first.status_code == 202

        for _ in range(100):
            state = owner.get(f"/api/jobs/{first.json()['id']}").json()
            if state["status"] == "failed":
                break
            time.sleep(0.01)

        assert state["status"] == "failed"
        cdk = store.verify(code)
        assert cdk["used_uses"] == 0
        assert cdk["reserved_uses"] == 0
        assert cdk["remaining_uses"] == 1

        second = owner.post("/api/jobs", json=payload)
        assert second.status_code == 202


def test_public_job_api_rejects_proxy_override() -> None:
    response = client.post(
        "/api/jobs",
        json={
            "cdk": "UPI-TEST-TEST-TEST",
            "credential": "x" * 80,
            "email": "owner@example.com",
            "proxy_pool": "http://should-not-be-accepted:2000",
            "authorized": False,
        },
    )
    assert response.status_code == 422
