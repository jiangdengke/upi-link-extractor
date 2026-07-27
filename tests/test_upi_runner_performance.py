from __future__ import annotations

import asyncio

import pytest

from upi_link.core import upi_runner
from upi_link.core.pay_upi_http import PayUpiError


def test_best_effort_warmups_use_short_timeouts() -> None:
    class FakeResponse:
        status_code = 200

    class FakeSession:
        def __init__(self) -> None:
            self.calls: list[tuple[str, float]] = []

        async def get(self, _url: str, **kwargs):
            self.calls.append(("get", kwargs["timeout"]))
            return FakeResponse()

        async def post(self, _url: str, **kwargs):
            self.calls.append(("post", kwargs["timeout"]))
            return FakeResponse()

    session = FakeSession()
    asyncio.run(
        upi_runner._warm_cf_cookie(
            session,
            proxies=None,
            log=lambda _message: None,
        )
    )
    asyncio.run(
        upi_runner._warm_sentinel_ping(
            session,
            access_token="token",
            proxy=None,
            log=lambda _message: None,
            context={},
        )
    )

    assert session.calls == [
        ("get", upi_runner.CF_WARMUP_TIMEOUT_SECONDS),
        ("post", upi_runner.SENTINEL_PING_TIMEOUT_SECONDS),
    ]
    assert upi_runner.CF_WARMUP_TIMEOUT_SECONDS == 4.0
    assert upi_runner.SENTINEL_PING_TIMEOUT_SECONDS == 6.0


def test_isolated_approve_can_use_sentinel_as_the_only_warmup(monkeypatch) -> None:
    calls: list[str] = []

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    monkeypatch.setattr(
        upi_runner,
        "_RotatingSession",
        lambda *_args, **_kwargs: FakeSession(),
    )
    monkeypatch.setattr(upi_runner, "_safe_materialize", lambda proxy: proxy)

    async def fake_cf_warm(*_args, **_kwargs):
        calls.append("cf")

    async def fake_ping(*_args, **_kwargs):
        calls.append("ping")

    async def fake_sentinel(*_args, **_kwargs):
        calls.append("sentinel")
        return "sentinel-token", "sentinel-so-token"

    async def fake_approve(*_args, **_kwargs):
        calls.append("approve")
        return {
            "http_status": 200,
            "ok": True,
            "result": "approved",
            "keys": ["result"],
        }

    monkeypatch.setattr(upi_runner, "_warm_cf_cookie", fake_cf_warm)
    monkeypatch.setattr(upi_runner, "_warm_sentinel_ping", fake_ping)
    monkeypatch.setattr(upi_runner, "_build_approve_sentinel", fake_sentinel)
    monkeypatch.setattr(upi_runner, "_chatgpt_approve_checkout", fake_approve)

    result = asyncio.run(
        upi_runner._approve_once_isolated(
            access_token="token",
            session_id="session",
            processor_entity="processor",
            raw_proxy="http://proxy.example:3000",
            warm=False,
        )
    )

    assert result["ok"] is True
    assert result["_materialized_proxy"] == "http://proxy.example:3000"
    assert calls == ["ping", "sentinel", "approve"]


def test_promo_bootstrap_failure_preserves_pay_upi_error(monkeypatch) -> None:
    class FakeSession:
        pass

    async def fake_warm(*_args, **_kwargs):
        return None

    async def fake_coupon(*_args, **_kwargs):
        return None

    async def fake_checkout(*_args, **_kwargs):
        return {
            "checkout_session_id": "session",
            "publishable_key": "key",
            "processor_entity": "processor",
        }

    async def fake_stripe_init(*_args, **_kwargs):
        raise RuntimeError("fixture bootstrap failure")

    monkeypatch.setattr(upi_runner, "_warm_cf_cookie", fake_warm)
    monkeypatch.setattr(upi_runner, "_check_promo_coupon", fake_coupon)
    monkeypatch.setattr(upi_runner, "_create_chatgpt_checkout", fake_checkout)
    monkeypatch.setattr(
        "upi_link.core.pay_upi_http._stripe_init",
        fake_stripe_init,
    )

    with pytest.raises(
        PayUpiError,
        match="promo bootstrap Stripe init failed: RuntimeError",
    ):
        asyncio.run(
            upi_runner._create_chatgpt_checkout_with_proxy_split(
                FakeSession(),
                access_token="token",
                upi_proxy="http://proxy.example:3000",
                promo_proxy="http://proxy.example:3000",
                log=lambda _message: None,
            )
        )
