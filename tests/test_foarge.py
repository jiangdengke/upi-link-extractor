from __future__ import annotations

import asyncio

from upi_link.credentials import Credential
from upi_link.extractor import ExtractionOptions
from upi_link.foarge import FoargeError, public_payment_state, run_foarge_payment


class FakeFoargeClient:
    def __init__(self, *, refresh: bool = False) -> None:
        self.refresh = refresh
        self.submitted: list[tuple[str, str]] = []
        self.refreshed: list[str] = []
        self.cancelled = False
        self.released = False
        self.polls = 0

    async def create_task(self, *, email: str, external_ref: str) -> dict:
        assert email == "owner@example.com"
        assert external_ref == "upi-job-1"
        return {"id": "task_1", "status": "queued", "queue_position": 2}

    async def get_task(self, task_id: str) -> dict:
        assert task_id == "task_1"
        self.polls += 1
        if self.polls == 1:
            return {"id": task_id, "status": "awaiting_checkout"}
        if self.refresh and self.polls == 2:
            return {
                "id": task_id,
                "status": "pending",
                "qr_needs_refresh": True,
                "refresh_count": 0,
            }
        if self.polls == 2:
            return {"id": task_id, "status": "assigned"}
        return {"id": task_id, "status": "completed"}

    async def submit_checkout(
        self,
        task_id: str,
        *,
        access_token: str,
        payment_link: str,
    ) -> dict:
        assert task_id == "task_1"
        self.submitted.append((access_token, payment_link))
        return {"id": task_id, "status": "pending"}

    async def refresh_checkout(self, task_id: str, *, payment_link: str) -> dict:
        assert task_id == "task_1"
        self.refreshed.append(payment_link)
        return {"id": task_id, "status": "assigned", "refresh_count": 1}

    async def cancel_task(self, task_id: str) -> None:
        assert task_id == "task_1"
        self.cancelled = True

    async def smart_release(self, task_id: str) -> None:
        assert task_id == "task_1"
        self.released = True


def test_foarge_payment_waits_for_promotion_then_completes(tmp_path) -> None:
    client = FakeFoargeClient()
    progress: list[dict] = []
    links: list[dict] = []
    extraction_calls = 0

    async def extract(credential, options, qr_path, log, should_cancel):
        nonlocal extraction_calls
        del options, log, should_cancel
        extraction_calls += 1
        qr_path.write_bytes(b"qr")
        return {
            "ok": True,
            "payment_link": "https://payments.stripe.com/upi/instructions/first",
            "qr_path": str(qr_path),
            "email": credential.email,
        }

    async def scenario() -> dict:
        return await run_foarge_payment(
            Credential("x" * 80, "owner@example.com"),
            ExtractionOptions(),
            tmp_path / "qr.png",
            lambda _message: None,
            lambda: False,
            client=client,
            external_ref="upi-job-1",
            extract=extract,
            on_progress=progress.append,
            on_link=links.append,
            poll_interval=0,
        )

    result = asyncio.run(scenario())
    assert result["ok"] is True
    assert result["payment_completed"] is True
    assert extraction_calls == 1
    assert len(links) == 1
    assert client.submitted == [
        (
            "x" * 80,
            "https://payments.stripe.com/upi/instructions/first",
        )
    ]
    assert [item["status"] for item in progress] == [
        "queued",
        "awaiting_checkout",
        "pending",
        "assigned",
        "completed",
    ]
    assert "x" * 80 not in repr(progress)


def test_foarge_payment_refreshes_expired_link_without_new_task(tmp_path) -> None:
    client = FakeFoargeClient(refresh=True)
    generated = 0
    links: list[dict] = []

    async def extract(credential, options, qr_path, log, should_cancel):
        nonlocal generated
        del credential, options, qr_path, log, should_cancel
        generated += 1
        return {
            "ok": True,
            "payment_link": f"https://payments.stripe.com/upi/instructions/link-{generated}",
        }

    async def scenario() -> dict:
        return await run_foarge_payment(
            Credential("y" * 80, "owner@example.com"),
            ExtractionOptions(),
            tmp_path / "qr.png",
            lambda _message: None,
            lambda: False,
            client=client,
            external_ref="upi-job-1",
            extract=extract,
            on_progress=lambda _state: None,
            on_link=links.append,
            poll_interval=0,
        )

    result = asyncio.run(scenario())
    assert result["ok"] is True
    assert generated == 2
    assert len(links) == 2
    assert client.refreshed == [
        "https://payments.stripe.com/upi/instructions/link-2"
    ]


def test_foarge_extraction_failure_cancels_upstream_reservation(tmp_path) -> None:
    client = FakeFoargeClient()

    async def extract(credential, options, qr_path, log, should_cancel):
        del credential, options, qr_path, log, should_cancel
        return {"ok": False, "error": "expected extraction failure"}

    async def scenario() -> dict:
        return await run_foarge_payment(
            Credential("q" * 80, "owner@example.com"),
            ExtractionOptions(),
            tmp_path / "qr.png",
            lambda _message: None,
            lambda: False,
            client=client,
            external_ref="upi-job-1",
            extract=extract,
            on_progress=lambda _state: None,
            on_link=lambda _result: None,
            poll_interval=0,
        )

    result = asyncio.run(scenario())
    assert result["ok"] is False
    assert client.cancelled is True
    assert client.submitted == []


def test_foarge_errors_cannot_echo_access_token(tmp_path) -> None:
    token = "secret-token-" + "x" * 80

    class FailingClient(FakeFoargeClient):
        async def create_task(self, *, email: str, external_ref: str) -> dict:
            del email, external_ref
            raise FoargeError(f"upstream echoed {token}", status_code=400)

    async def scenario() -> dict:
        return await run_foarge_payment(
            Credential(token, "owner@example.com"),
            ExtractionOptions(),
            tmp_path / "qr.png",
            lambda _message: None,
            lambda: False,
            client=FailingClient(),
            external_ref="upi-job-1",
            extract=lambda *args: None,
            on_progress=lambda _state: None,
            on_link=lambda _result: None,
            poll_interval=0,
        )

    result = asyncio.run(scenario())
    assert result["ok"] is False
    assert token not in repr(result)


def test_public_payment_state_treats_either_expiry_flag_as_refresh() -> None:
    state = public_payment_state(
        {
            "id": "task_1",
            "status": "pending",
            "qr_needs_refresh": False,
            "qr_expired": True,
        }
    )
    assert state["qr_needs_refresh"] is True
