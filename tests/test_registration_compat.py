from __future__ import annotations

import base64
import json

from fastapi.testclient import TestClient

import main
from upi_link.cdk import CdkStore
from upi_link.jobs import JobManager
from upi_link.settings import SettingsStore


def _access_token(email: str) -> str:
    def encode(value: dict) -> str:
        raw = json.dumps(value, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return f"{encode({'alg': 'none'})}.{encode({'email': email})}.fixture"


def _event_payload(stream: str, event_name: str) -> dict:
    event = ""
    data_lines: list[str] = []
    for line in stream.splitlines() + [""]:
        if not line:
            if event == event_name and data_lines:
                return json.loads("\n".join(data_lines))
            event = ""
            data_lines = []
        elif line.startswith("event:"):
            event = line.split(":", 1)[1].strip()
        elif line.startswith("data:"):
            data_lines.append(line.split(":", 1)[1].lstrip())
    raise AssertionError(f"event {event_name!r} was not present")


def test_registration_compatibility_contract(monkeypatch, tmp_path) -> None:
    store = CdkStore(tmp_path / "compat.db")
    code = store.generate(count=1, max_uses=1, expires_in_days=30)[0]["code"]
    other_code = store.generate(count=1, max_uses=1, expires_in_days=30)[0]["code"]
    token = _access_token("owner@example.com")

    async def fake_runner(credential, options, qr_path, log, should_cancel):
        del options, should_cancel
        log(f"processing {credential.email}")
        qr_path.write_bytes(b"fake-png")
        return {
            "ok": True,
            "email": credential.email,
            "amount": 0,
            "payment_link": "https://payments.stripe.com/upi/instructions/compat",
            "qr_path": str(qr_path),
            "qr_expires_at": 2_000_000_000,
            "elapsed_seconds": 1.25,
        }

    manager = JobManager(tmp_path / "qr", runner=fake_runner)
    monkeypatch.setattr(main, "cdks", store)
    monkeypatch.setattr(main, "settings", SettingsStore(tmp_path / "compat.db"))
    monkeypatch.setattr(main, "jobs", manager)

    with TestClient(main.app) as creator:
        verified = creator.get("/api/cdk", params={"code": code})
        assert verified.status_code == 200
        assert verified.json()["ok"] is True
        assert verified.json()["remaining_uses"] == 1

        created = creator.post(
            "/api/extract",
            json={"token": token, "link_type": "upi", "cdk": code},
        )
        assert created.status_code == 202
        assert created.json()["link_type"] == "upi"
        assert created.json()["cdk_remaining"] == 0
        job_id = created.json()["job_id"]

    # The registration client creates a fresh HTTP session for the event stream.
    with TestClient(main.app) as watcher:
        wrong_owner = watcher.get(
            f"/api/jobs/{job_id}/events",
            params={"cdk": other_code},
        )
        assert wrong_owner.status_code == 404

        events = watcher.get(
            f"/api/jobs/{job_id}/events",
            params={"cdk": code},
        )
        assert events.status_code == 200
        assert "event: log" in events.text
        assert "event: result" in events.text
        assert "event: done" in events.text
        assert token not in events.text

        payload = _event_payload(events.text, "result")["result"]
        assert payload["long_url"].endswith("/compat")
        assert payload["copy_paste"] == payload["long_url"]
        assert payload["payment_method"] == "UPI"
        assert payload["payment_link_type"] == "upi"
        assert payload["expires_at"] == 2_000_000_000
        assert payload["cdk_remaining"] == 0
        assert payload["image_url_png"].startswith(
            f"http://testserver/api/jobs/{job_id}/qr?access="
        )

        # Signed QR URLs remain usable after the in-memory job list is trimmed.
        manager._jobs.pop(job_id)
        qr = watcher.get(payload["image_url_png"])
        assert qr.status_code == 200
        assert qr.content == b"fake-png"
        assert watcher.get(f"/api/jobs/{job_id}/qr?access=invalid").status_code == 404

    assert store.verify(code)["used_uses"] == 1


def test_registration_compatibility_streams_failures(monkeypatch, tmp_path) -> None:
    store = CdkStore(tmp_path / "failed.db")
    code = store.generate(count=1, max_uses=1, expires_in_days=30)[0]["code"]

    async def failed_runner(credential, options, qr_path, log, should_cancel):
        del credential, options, qr_path, log, should_cancel
        return {"ok": False, "error": "expected compatibility failure"}

    monkeypatch.setattr(main, "cdks", store)
    monkeypatch.setattr(main, "settings", SettingsStore(tmp_path / "failed.db"))
    monkeypatch.setattr(
        main,
        "jobs",
        JobManager(tmp_path / "failed-qr", runner=failed_runner),
    )

    with TestClient(main.app) as client:
        rejected = client.post(
            "/api/extract",
            json={
                "token": _access_token("owner@example.com"),
                "link_type": "pix",
                "cdk": code,
            },
        )
        assert rejected.status_code == 400
        assert "仅支持 UPI" in rejected.json()["error"]

        invalid_cdk = client.post(
            "/api/extract",
            json={
                "token": _access_token("owner@example.com"),
                "link_type": "upi",
                "cdk": "UPI-NOT-A-REAL-CDK",
            },
        )
        assert invalid_cdk.status_code == 403
        assert invalid_cdk.json()["error"] == "CDK 不存在"

        created = client.post(
            "/api/extract",
            json={
                "token": _access_token("owner@example.com"),
                "link_type": "upi",
                "cdk": code,
            },
        )
        assert created.status_code == 202
        events = client.get(
            f"/api/jobs/{created.json()['job_id']}/events",
            params={"cdk": code},
        )

    assert events.status_code == 200
    assert "event: error" in events.text
    assert "expected compatibility failure" in events.text
    assert "event: done" in events.text
    assert store.verify(code)["remaining_uses"] == 1
