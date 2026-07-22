from __future__ import annotations

import asyncio
from pathlib import Path

from upi_link.credentials import Credential
from upi_link.extractor import ExtractionOptions
from upi_link.jobs import JobManager


def test_job_manager_runs_without_retaining_secret(tmp_path: Path) -> None:
    secret = "x" * 80

    async def fake_runner(credential, options, qr_path, log, should_cancel):
        del options, should_cancel
        log(f"token={credential.access_token}")
        qr_path.write_bytes(b"fake-png")
        return {"ok": True, "payment_link": "https://payments.stripe.com/upi/instructions/test", "qr_path": str(qr_path)}

    async def scenario() -> dict:
        manager = JobManager(tmp_path, runner=fake_runner)
        created = manager.create(Credential(secret, "owner@example.com"), ExtractionOptions())
        for _ in range(100):
            snapshot = manager.get(created["id"]).snapshot()
            if snapshot["status"] not in {"queued", "running"}:
                return snapshot
            await asyncio.sleep(0.01)
        raise AssertionError("job did not finish")

    snapshot = asyncio.run(scenario())
    assert snapshot["status"] == "success"
    assert secret not in json_like(snapshot)
    assert snapshot["result"]["qr_url"].endswith("/qr")
    assert snapshot["result"]["generated_at"]


def json_like(value) -> str:
    return repr(value)


def test_job_manager_isolates_owners_and_calls_completion_once(tmp_path: Path) -> None:
    completed: list[str] = []

    async def fake_runner(credential, options, qr_path, log, should_cancel):
        del credential, options, qr_path, log, should_cancel
        return {"ok": False, "error": "expected"}

    async def scenario() -> None:
        manager = JobManager(tmp_path, runner=fake_runner)
        created = manager.create(
            Credential("z" * 80, "owner@example.com"),
            ExtractionOptions(),
            owner_id="owner-a",
            on_complete=lambda job: completed.append(job.id),
        )
        assert manager.get(created["id"], owner_id="owner-b") is None
        assert manager.list(owner_id="owner-b") == []
        for _ in range(100):
            job = manager.get(created["id"], owner_id="owner-a")
            if job and job.status == "failed":
                break
            await asyncio.sleep(0.01)

    asyncio.run(scenario())
    assert len(completed) == 1
