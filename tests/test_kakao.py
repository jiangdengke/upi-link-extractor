from __future__ import annotations

import asyncio
import time
from pathlib import Path
from urllib.parse import unquote, urlsplit

from fastapi.testclient import TestClient

import main
from upi_link import extractor
from upi_link.cdk import CdkStore
from upi_link.core import kakao_runner
from upi_link.credentials import Credential, parse_credential
from upi_link.extractor import ExtractionOptions
from upi_link.jobs import JobManager
from upi_link.settings import SettingsStore


def test_country_proxy_derivation_preserves_sticky_session() -> None:
    seed = (
        "http://account-region-IN-city-Patna-sid-sticky123-t-5:"
        "password@proxy.example:3000"
    )

    checkout, promotion, provider = kakao_runner._proxy_chain(seed)

    checkout_user = unquote(urlsplit(checkout).username or "")
    promotion_user = unquote(urlsplit(promotion).username or "")
    provider_user = unquote(urlsplit(provider).username or "")
    assert "region-KR" in checkout_user
    assert "region-VN" in promotion_user
    assert "region-KR" in provider_user
    assert all(
        "sid-sticky123" in username
        for username in (checkout_user, promotion_user, provider_user)
    )


def test_four_part_proxy_seed_is_supported() -> None:
    seed = (
        "proxy.example:3000:"
        "account-region-IN-city-Patna-sid-sticky123-t-5:password"
    )

    checkout, promotion, provider = kakao_runner._proxy_chain(seed)

    assert urlsplit(checkout).hostname == "proxy.example"
    assert urlsplit(checkout).port == 3000
    assert "region-KR" in unquote(urlsplit(checkout).username or "")
    assert "region-VN" in unquote(urlsplit(promotion).username or "")
    assert "region-KR" in unquote(urlsplit(provider).username or "")


def test_sid_template_uses_one_kr_session_and_a_separate_promotion_session() -> None:
    seed = (
        "proxy.example:3000:"
        "account-region-IN-sid-{SID}-t-30:password"
    )

    checkout, promotion, provider = kakao_runner._materialized_proxy_chain(seed)

    checkout_user = unquote(urlsplit(checkout).username or "")
    promotion_user = unquote(urlsplit(promotion).username or "")
    provider_user = unquote(urlsplit(provider).username or "")
    assert "{SID}" not in checkout_user + promotion_user + provider_user
    assert checkout_user == provider_user
    assert promotion_user != checkout_user
    assert "region-KR" in checkout_user
    assert "region-VN" in promotion_user


def test_kakao_extractor_dispatches_to_korean_runner(
    monkeypatch,
    tmp_path: Path,
) -> None:
    calls: list[dict] = []

    def fake_runner(token: str, **kwargs) -> dict:
        calls.append({"token": token, **kwargs})
        return {
            "provider_redirect_url": "https://web.nicepay.co.kr/pay/test",
            "stripe_redirect_url": "https://checkout.stripe.com/redirect/test",
            "checkout_session_id": "cs_test",
            "payment_method_id": "pm_test",
            "amount": "0",
        }

    monkeypatch.setattr(extractor, "run_kakao_link", fake_runner)
    result = asyncio.run(
        extractor.extract_upi_link(
            Credential("x" * 80, ""),
            ExtractionOptions(
                link_type="kakao",
                kakao_proxy_pool=("http://proxy.example:3000",),
                approve_retries=7,
            ),
            tmp_path / "unused.png",
            lambda _message: None,
            lambda: False,
        )
    )

    assert result["ok"] is True
    assert result["link_type"] == "kakao"
    assert result["payment_method"] == "Kakao Pay"
    assert result["payment_link"] == "https://web.nicepay.co.kr/pay/test"
    assert result["qr_path"] == ""
    assert calls[0]["proxy_pool"] == ("http://proxy.example:3000",)
    assert calls[0]["approve_retries"] == 7


def test_kakao_credential_can_omit_email() -> None:
    credential = parse_credential("x" * 80, require_email=False)
    assert credential.email == ""


def test_compatibility_api_accepts_kakao_without_email(
    monkeypatch,
    tmp_path: Path,
) -> None:
    store = CdkStore(tmp_path / "kakao-api.db")
    code = store.generate(count=1, max_uses=1, expires_in_days=30)[0]["code"]
    seen_types: list[str] = []

    async def fake_runner(credential, options, qr_path, log, should_cancel):
        del credential, qr_path, log, should_cancel
        seen_types.append(options.link_type)
        return {
            "ok": True,
            "link_type": "kakao",
            "payment_method": "Kakao Pay",
            "payment_link": "https://web.nicepay.co.kr/pay/test",
        }

    monkeypatch.setattr(main, "cdks", store)
    monkeypatch.setattr(main, "settings", SettingsStore(tmp_path / "kakao-api.db"))
    monkeypatch.setattr(
        main,
        "jobs",
        JobManager(tmp_path / "kakao-qr", runner=fake_runner),
    )

    with TestClient(main.app) as client:
        response = client.post(
            "/api/extract",
            json={"token": "x" * 80, "link_type": "kakao", "cdk": code},
        )
        assert response.status_code == 202
        assert response.json()["link_type"] == "kakao"
        for _ in range(50):
            if seen_types:
                break
            time.sleep(0.01)
    assert seen_types == ["kakao"]
