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


def json_like(value) -> str:
    return repr(value)

