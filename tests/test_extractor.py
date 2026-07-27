from __future__ import annotations

import asyncio
from pathlib import Path

import upi_link.extractor as extractor
from upi_link.core import UpiQrResult
from upi_link.credentials import Credential
from upi_link.extractor import ExtractionOptions


def test_blocked_attempts_rotate_checkout_with_one_total_budget(
    monkeypatch,
    tmp_path: Path,
) -> None:
    calls: list[dict] = []

    async def fake_probe(**kwargs) -> UpiQrResult:
        calls.append(kwargs)
        attempt_count = min(10, kwargs["approve_retries"])
        cycle = len(calls)
        return UpiQrResult(
            ok=cycle == 3,
            email="o***@example.com",
            payment_link=(
                "https://payments.stripe.com/upi/instructions/test"
                if cycle == 3
                else None
            ),
            approve_attempts=[
                {"result": "blocked", "attempt": index + 1}
                for index in range(attempt_count)
            ],
            relogin_requested=cycle < 3,
            elapsed_seconds=float(cycle),
        )

    monkeypatch.setattr(extractor, "run_upi_qr_probe", fake_probe)
    logs: list[str] = []
    result = asyncio.run(
        extractor.extract_upi_link(
            Credential("x" * 80, "owner@example.com"),
            ExtractionOptions(approve_retries=30),
            tmp_path / "qr.png",
            logs.append,
            lambda: False,
        )
    )

    assert result["ok"] is True
    assert result["elapsed_seconds"] == 6.0
    assert [call["approve_retries"] for call in calls] == [30, 20, 10]
    assert [call["force_fresh"] for call in calls] == [False, True, True]
    assert [call["relogin_block_streak"] for call in calls] == [10, 10, 0]
    assert all(call["restart_threshold"] == 3 for call in calls)
    assert all(call["max_restarts"] == 2 for call in calls)
    assert len([line for line in logs if "recovery cycle" in line]) == 2


def test_non_block_failure_does_not_start_an_outer_cycle(
    monkeypatch,
    tmp_path: Path,
) -> None:
    calls = 0

    async def fake_probe(**kwargs) -> UpiQrResult:
        nonlocal calls
        calls += 1
        return UpiQrResult(
            ok=False,
            email="o***@example.com",
            error="confirm failed for every variant",
            elapsed_seconds=1.5,
        )

    monkeypatch.setattr(extractor, "run_upi_qr_probe", fake_probe)
    result = asyncio.run(
        extractor.extract_upi_link(
            Credential("x" * 80, "owner@example.com"),
            ExtractionOptions(),
            tmp_path / "qr.png",
            lambda _message: None,
            lambda: False,
        )
    )

    assert result["ok"] is False
    assert result["error"] == "confirm failed for every variant"
    assert calls == 1
