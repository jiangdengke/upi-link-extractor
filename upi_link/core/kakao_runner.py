"""Kakao Pay / Nicepay checkout-link extraction.

The Korean flow is intentionally isolated from the UPI runner.  It uses one
proxy seed for the checkout, promotion and provider stages, while preserving
the provider's country/region selector when one is present in the credential.
"""

from __future__ import annotations

import os
import random
import re
import time
import uuid
from collections.abc import Callable
from typing import Any, Protocol
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit

import requests

try:
    from curl_cffi.requests import Session as CurlCffiSession
except ImportError:  # pragma: no cover - requests is the fallback in tests
    CurlCffiSession = None


LogFn = Callable[[str], None]
CancelFn = Callable[[], bool]

TIMEOUT = max(5, min(120, int(os.environ.get("KAKAO_PAY_TIMEOUT", "30") or "30")))
POLL_TIMEOUT = max(5, min(300, int(os.environ.get("KAKAO_POLL_TIMEOUT", "120") or "120")))
APPROVE_RETRY_MAX = max(
    1,
    min(10, int(os.environ.get("KAKAO_APPROVE_RETRY_MAX", "1") or "1")),
)
STRIPE_VERSION = "2025-03-31.basil; checkout_server_update_beta=v1; checkout_manual_approval_preview=v1"
STRIPE_RUNTIME = "c00af4ce81"
STRIPE_PAYMENT_UA = f"stripe.js/{STRIPE_RUNTIME}; stripe-js-v3/{STRIPE_RUNTIME}; checkout"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
)
IP_CHECK_SOURCES = (
    ("https://ipinfo.io/json", "country"),
    ("https://ipapi.co/json/", "country_code"),
    ("https://ipwho.is/", "country_code"),
)
CHECKOUT_COUNTRY = os.environ.get("KAKAO_BOOTSTRAP_COUNTRY", "KR").strip().upper() or "KR"
PROMOTION_COUNTRY = os.environ.get("KAKAO_PROMOTION_COUNTRY", "VN").strip().upper() or "VN"
PROVIDER_COUNTRY = os.environ.get("KAKAO_PROVIDER_COUNTRY", "KR").strip().upper() or "KR"
_COUNTRY_SELECTOR_RE = re.compile(
    r"(?i)(?P<name>country|region)(?P<separator>[-_=])(?P<value>[a-z]{2}(?:,[a-z]{2})*)"
)

KOREAN_FAMILY_NAMES = (
    "김", "이", "박", "최", "정", "강", "조", "윤", "장", "임", "한", "오", "서", "신", "권", "황",
)
KOREAN_GIVEN_NAMES = (
    "민준", "서준", "도윤", "예준", "시우", "주원", "하준", "지호", "지후", "준서", "서연", "서윤",
    "지우", "서현", "하은", "하윤", "민서", "지유", "윤서", "채원",
)
SEOUL_ADDRESS_SEEDS = (
    {"district": "강남구", "road": "테헤란로", "postal": "06164", "base": 87, "span": 40},
    {"district": "강남구", "road": "봉은사로", "postal": "06097", "base": 524, "span": 32},
    {"district": "서초구", "road": "서초대로", "postal": "06611", "base": 396, "span": 36},
    {"district": "송파구", "road": "올림픽로", "postal": "05510", "base": 300, "span": 36},
    {"district": "마포구", "road": "월드컵북로", "postal": "03925", "base": 396, "span": 36},
)
EMAIL_DOMAINS = ("gmail.com", "naver.com", "daum.net", "kakao.com")


class KakaoError(RuntimeError):
    """Raised when the Kakao checkout cannot produce a provider redirect."""


class StopSignal(Protocol):
    def is_set(self) -> bool: ...


class _CallbackStopSignal:
    def __init__(self, callback: CancelFn) -> None:
        self.callback = callback

    def is_set(self) -> bool:
        return self.callback()


def _check_running(stop_event: StopSignal | None) -> None:
    if stop_event is not None and stop_event.is_set():
        raise KakaoError("任务已停止")


def _proxy_url(raw: str) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    if "://" not in text:
        if text.count(":") == 3 and "@" not in text:
            host, port, username, password = text.split(":", 3)
            text = (
                f"http://{quote(username, safe='-._~')}:"
                f"{quote(password, safe='-._~')}@{host}:{port}"
            )
        else:
            text = f"http://{text}"
    try:
        parsed = urlsplit(text)
        port = parsed.port
    except ValueError as exc:
        raise KakaoError("韩国代理端口格式无效") from exc
    if not parsed.hostname:
        raise KakaoError("韩国代理格式无效")
    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    if port:
        host = f"{host}:{port}"
    username = quote(unquote(parsed.username or ""), safe="-._~")
    auth = username
    if parsed.password is not None:
        auth = f"{auth}:{quote(unquote(parsed.password), safe='-._~')}"
    netloc = f"{auth}@{host}" if auth else host
    return urlunsplit((parsed.scheme.lower(), netloc, parsed.path, parsed.query, parsed.fragment))


def _proxy_for_country(proxy: str, country: str) -> str:
    """Rewrite a country/region selector while retaining sticky session data."""
    normalized = _proxy_url(proxy)
    if not normalized:
        return ""
    parsed = urlsplit(normalized)
    username = unquote(parsed.username or "")
    password = unquote(parsed.password or "")
    target = str(country or "").strip().lower()
    replacements = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal replacements
        replacements += 1
        value = target.upper() if match.group("value").isupper() else target
        return f"{match.group('name')}{match.group('separator')}{value}"

    username = _COUNTRY_SELECTOR_RE.sub(replace, username)
    password = _COUNTRY_SELECTOR_RE.sub(replace, password)
    if not replacements:
        return normalized
    host = parsed.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    if parsed.port:
        host = f"{host}:{parsed.port}"
    auth = quote(username, safe="-._~")
    if parsed.password is not None:
        auth = f"{auth}:{quote(password, safe='-._~')}"
    return urlunsplit((parsed.scheme, f"{auth}@{host}", parsed.path, parsed.query, parsed.fragment))


def _proxy_chain(proxy_seed: str) -> tuple[str, str, str]:
    return (
        _proxy_for_country(proxy_seed, CHECKOUT_COUNTRY),
        _proxy_for_country(proxy_seed, PROMOTION_COUNTRY),
        _proxy_for_country(proxy_seed, PROVIDER_COUNTRY),
    )


def _materialized_proxy_chain(proxy_seed: str) -> tuple[str, str, str]:
    """Materialize proxy templates without letting one sticky SID pin two regions."""
    if "{SID}" not in proxy_seed:
        return _proxy_chain(proxy_seed)
    kr_seed = proxy_seed.replace("{SID}", uuid.uuid4().hex[:12])
    promotion_seed = proxy_seed.replace("{SID}", uuid.uuid4().hex[:12])
    return (
        _proxy_for_country(kr_seed, CHECKOUT_COUNTRY),
        _proxy_for_country(promotion_seed, PROMOTION_COUNTRY),
        _proxy_for_country(kr_seed, PROVIDER_COUNTRY),
    )


def _redact_proxy_error(text: str, *proxies: str) -> str:
    safe = str(text or "")
    values: set[str] = set()
    for proxy in proxies:
        raw = str(proxy or "").strip()
        if not raw:
            continue
        values.add(raw)
        if raw.count(":") == 3 and "@" not in raw:
            _host, port, username, password = raw.split(":", 3)
            values.update(
                {
                    f"{port}:{username}:{password}",
                    username,
                    password,
                    f"{username}:{password}",
                }
            )
        try:
            normalized = _proxy_url(raw)
        except (TypeError, ValueError, KakaoError):
            normalized = ""
        if normalized:
            values.add(normalized)
            values.add(unquote(normalized))
            parsed = urlsplit(unquote(normalized))
            if parsed.netloc:
                values.add(parsed.netloc)
    for value in sorted(values, key=len, reverse=True):
        safe = safe.replace(value, "[KAKAO_PROXY]")
    return safe


def _new_session(proxy: str) -> Any:
    session: Any
    if CurlCffiSession is not None:
        session = CurlCffiSession(impersonate="chrome136")
    else:
        session = requests.Session()
    if hasattr(session, "trust_env"):
        session.trust_env = False
    session.proxies = {"http": proxy, "https": proxy} if proxy else {}
    return session


def _preflight_enabled() -> bool:
    return os.environ.get("KAKAO_PROXY_PREFLIGHT", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def _exit_country(proxy: str) -> str:
    session = _new_session(proxy)
    errors: list[str] = []
    for url, country_key in IP_CHECK_SOURCES:
        try:
            response = session.get(
                url,
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                timeout=min(12, TIMEOUT),
            )
            if response.status_code >= 400:
                errors.append(f"{urlsplit(url).netloc} HTTP {response.status_code}")
                continue
            payload = response.json() or {}
            country = str(payload.get(country_key) or payload.get("country") or "").upper()
            if country:
                return country
            errors.append(f"{urlsplit(url).netloc} no country")
        except Exception as exc:  # noqa: BLE001 - try the next IP service
            errors.append(f"{urlsplit(url).netloc} {str(exc)[:80]}")
    raise KakaoError("韩国代理出口检测失败：" + "；".join(errors))


def _response_error(response: Any, limit: int = 800) -> str:
    try:
        return str(response.text or "")[:limit]
    except Exception:  # noqa: BLE001 - response implementations vary
        return ""


def _stripe_headers(publishable_key: str, referer: str) -> dict[str, str]:
    origin = "https://checkout.stripe.com" if "checkout.stripe.com" in referer else "https://pay.openai.com"
    return {
        "Authorization": f"Bearer {publishable_key}",
        "Origin": origin,
        "Referer": referer,
        "Accept": "application/json",
        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8",
        "Sec-Fetch-Site": "same-site" if origin == "https://checkout.stripe.com" else "cross-site",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": USER_AGENT,
    }


def _elements_params(stripe_js_id: str, session_id: str = "") -> dict[str, str]:
    params = {
        "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
        "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
        "elements_session_client[elements_init_source]": "custom_checkout",
        "elements_session_client[referrer_host]": "chatgpt.com",
        "elements_session_client[stripe_js_id]": stripe_js_id,
        "elements_session_client[locale]": "ko",
        "elements_session_client[is_aggregation_expected]": "false",
        "elements_options_client[saved_payment_method][enable_save]": "auto",
        "elements_options_client[saved_payment_method][enable_redisplay]": "auto",
    }
    if session_id:
        params["elements_session_client[session_id]"] = session_id
    return params


def _checkout_page_url(checkout_id: str, checkout: dict[str, Any]) -> str:
    processor = str(checkout.get("processor_entity") or "openai_llc")
    return f"https://chatgpt.com/checkout/{processor}/{checkout_id}"


def _checkout_headers(token: str, referer: str, target_path: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "oai-language": "ko-KR",
        "User-Agent": USER_AGENT,
        "Referer": referer,
        "x-openai-target-path": target_path,
        "x-openai-target-route": target_path,
    }


def _create_checkout(session: Any, token: str) -> tuple[str, str, dict[str, Any]]:
    payload: dict[str, Any] = {
        "plan_name": "chatgptplusplan",
        "billing_details": {"country": CHECKOUT_COUNTRY, "currency": "KRW"},
        "cancel_url": "https://chatgpt.com/#pricing",
        "checkout_ui_mode": "custom",
    }
    promo_mode = os.environ.get("KAKAO_PROMO_MODE", "campaign").strip().lower()
    promo_id = os.environ.get("KAKAO_PROMO_ID", "plus-1-month-free").strip()
    if promo_mode != "off" and promo_id:
        payload["promo_campaign"] = {
            "promo_campaign_id": promo_id,
            "is_coupon_from_query_param": False,
        }
    response = session.post(
        "https://chatgpt.com/backend-api/payments/checkout",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "oai-language": "ko-KR",
            "User-Agent": USER_AGENT,
        },
        json=payload,
        timeout=TIMEOUT,
    )
    if response.status_code != 200:
        raise KakaoError(f"checkout failed {response.status_code}: {_response_error(response)}")
    checkout = response.json() or {}
    checkout_id = str(checkout.get("checkout_session_id") or "")
    publishable_key = str(checkout.get("publishable_key") or "")
    if not checkout_id or not publishable_key:
        raise KakaoError("checkout 响应缺少 checkout_session_id 或 publishable_key")
    return checkout_id, publishable_key, checkout


def _activate_checkout(session: Any, checkout_id: str) -> str:
    checkout_page = f"https://checkout.stripe.com/c/pay/{checkout_id}"
    for url in (f"https://pay.openai.com/c/pay/{checkout_id}", checkout_page):
        session.get(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,*/*",
                "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8",
                "Referer": "https://chatgpt.com/",
            },
            timeout=TIMEOUT,
        )
    return checkout_page


def _stripe_init(
    session: Any,
    checkout_id: str,
    publishable_key: str,
    checkout_page: str,
) -> tuple[dict[str, Any], str]:
    stripe_js_id = str(uuid.uuid4())
    response = session.post(
        f"https://api.stripe.com/v1/payment_pages/{checkout_id}/init",
        data={
            "key": publishable_key,
            "eid": "NA",
            "browser_locale": "ko-KR",
            "browser_timezone": "Asia/Seoul",
            "redirect_type": "url",
            "_stripe_version": STRIPE_VERSION,
            **_elements_params(stripe_js_id),
        },
        headers=_stripe_headers(publishable_key, checkout_page),
        timeout=TIMEOUT,
    )
    if response.status_code != 200:
        raise KakaoError(f"stripe init failed {response.status_code}: {_response_error(response)}")
    payload = response.json() or {}
    if not isinstance(payload, dict):
        raise KakaoError("stripe init 返回格式无效")
    return payload, stripe_js_id


def _expected_amount(payload: dict[str, Any]) -> str:
    options = payload.get("elements_options") if isinstance(payload.get("elements_options"), dict) else {}
    if options.get("amount") is not None:
        return str(int(options["amount"]))
    summary = payload.get("total_summary") if isinstance(payload.get("total_summary"), dict) else {}
    if summary.get("due") is not None:
        return str(int(summary["due"]))
    invoice = payload.get("invoice") if isinstance(payload.get("invoice"), dict) else {}
    for name in ("amount_due", "total"):
        if invoice.get(name) is not None:
            return str(int(invoice[name]))
    line_items = payload.get("line_items")
    if isinstance(line_items, list):
        values = [
            item.get("amount")
            for item in line_items
            if isinstance(item, dict) and item.get("amount") is not None
        ]
        if values:
            return str(sum(int(value) for value in values))
    return "unknown"


def _inspect_init(payload: dict[str, Any], stage: str, *, require_zero: bool, log: LogFn) -> str:
    amount = _expected_amount(payload)
    currency = str(payload.get("currency") or "").lower()
    methods = [str(item).lower() for item in (payload.get("payment_method_types") or [])]
    log(f"{stage} Stripe init: amount={amount}; currency={currency}; methods={','.join(methods) or 'none'}")
    if "kakao_pay" not in methods or (require_zero and (amount != "0" or currency != "krw")):
        raise KakaoError(
            f"checkout_not_kakao_trial: stage={stage} amount={amount} currency={currency} methods={methods}"
        )
    return amount


def _update_promotion(session: Any, token: str, checkout_id: str, checkout: dict[str, Any]) -> None:
    promo_mode = os.environ.get("KAKAO_PROMO_MODE", "campaign").strip().lower()
    promo_id = os.environ.get("KAKAO_PROMO_ID", "plus-1-month-free").strip()
    body: dict[str, Any] = {
        "checkout_session_id": checkout_id,
        "processor_entity": str(checkout.get("processor_entity") or "openai_llc"),
        "plan_name": "chatgptplusplan",
        "price_interval": "month",
        "seat_quantity": 1,
    }
    if promo_mode != "off" and promo_id:
        body["promo_campaign"] = {
            "promo_campaign_id": promo_id,
            "is_coupon_from_query_param": False,
        }
    target_path = "/backend-api/payments/checkout/update"
    response = session.post(
        f"https://chatgpt.com{target_path}",
        headers=_checkout_headers(token, _checkout_page_url(checkout_id, checkout), target_path),
        json=body,
        timeout=TIMEOUT,
    )
    if response.status_code >= 400:
        raise KakaoError(f"checkout/update failed {response.status_code}: {_response_error(response)}")
    payload = response.json() if response.content else {}
    if isinstance(payload, dict) and payload.get("success") is False:
        raise KakaoError("checkout/update 被上游拒绝")


def _billing(token: str) -> dict[str, str]:
    seed = f"{token}:{uuid.uuid4()}".encode()
    rng = random.Random(seed)
    address = rng.choice(SEOUL_ADDRESS_SEEDS)
    name = f"{rng.choice(KOREAN_FAMILY_NAMES)}{rng.choice(KOREAN_GIVEN_NAMES)}"
    local_name = uuid.uuid5(uuid.NAMESPACE_DNS, name + str(uuid.uuid4())).hex[:10]
    return {
        "name": name,
        "email": f"{local_name}@{rng.choice(EMAIL_DOMAINS)}",
        "line1": f"{address['road']} {address['base'] + rng.randrange(address['span'])}",
        "line2": "",
        "city": "서울특별시",
        "state": str(address["district"]),
        "postal_code": str(address["postal"]),
        "country": PROVIDER_COUNTRY,
    }


def _update_taxes(
    session: Any,
    token: str,
    checkout_id: str,
    checkout: dict[str, Any],
    billing: dict[str, str],
) -> None:
    target_path = "/backend-api/payments/checkout/taxes"
    response = session.post(
        f"https://chatgpt.com{target_path}",
        headers=_checkout_headers(token, _checkout_page_url(checkout_id, checkout), target_path),
        json={
            "checkout_session_id": checkout_id,
            "checkout_email": billing["email"],
            "billing_country": PROVIDER_COUNTRY,
            "billing_name": billing["name"],
            "currency": "KRW",
            "tax_id": None,
            "processor_entity": str(checkout.get("processor_entity") or "openai_llc"),
            "billing_address": {
                "line1": billing["line1"],
                "city": billing["city"],
                "country": PROVIDER_COUNTRY,
                "postal_code": billing["postal_code"],
                "state": billing["state"],
            },
        },
        timeout=TIMEOUT,
    )
    if response.status_code >= 400:
        raise KakaoError(f"checkout/taxes failed {response.status_code}: {_response_error(response)}")


def _update_tax_region(
    session: Any,
    checkout_id: str,
    publishable_key: str,
    checkout_page: str,
    stripe_js_id: str,
    billing: dict[str, str],
) -> None:
    elements_session_id = f"elements_session_{uuid.uuid4().hex[:11]}"
    response = session.post(
        f"https://api.stripe.com/v1/payment_pages/{checkout_id}",
        data={
            "key": publishable_key,
            "_stripe_version": STRIPE_VERSION,
            **_elements_params(stripe_js_id, elements_session_id),
            "tax_region[country]": billing["country"],
            "tax_region[postal_code]": billing["postal_code"],
            "tax_region[line1]": billing["line1"],
            "tax_region[city]": billing["city"],
            "tax_region[state]": billing["state"],
        },
        headers=_stripe_headers(publishable_key, checkout_page),
        timeout=TIMEOUT,
    )
    if response.status_code >= 400:
        raise KakaoError(f"tax_region failed {response.status_code}: {_response_error(response)}")


def _extract_redirect(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    action = payload.get("next_action")
    if isinstance(action, dict) and action.get("type") == "redirect_to_url":
        redirect = action.get("redirect_to_url") or {}
        if isinstance(redirect, dict) and redirect.get("url"):
            return str(redirect["url"])
    for name in ("setup_intent", "payment_intent"):
        redirect = _extract_redirect(payload.get(name))
        if redirect:
            return redirect
    return ""


def _kakao_link(
    token: str,
    checkout_proxy: str,
    promotion_proxy: str,
    provider_proxy: str,
    *,
    approve_retries: int,
    log: LogFn,
    stop_event: StopSignal | None,
) -> dict[str, Any]:
    _check_running(stop_event)
    checkout_session = _new_session(checkout_proxy)
    promotion_session = _new_session(promotion_proxy)
    provider_session = _new_session(provider_proxy)

    me = checkout_session.get(
        "https://chatgpt.com/backend-api/me",
        headers={"Authorization": f"Bearer {token}", "User-Agent": USER_AGENT},
        timeout=TIMEOUT,
    )
    if me.status_code != 200:
        raise KakaoError(f"ChatGPT /me failed {me.status_code}: {_response_error(me, 500)}")

    checkout_id, publishable_key, checkout = _create_checkout(checkout_session, token)
    checkout_page = _activate_checkout(checkout_session, checkout_id)
    bootstrap_payload, _ = _stripe_init(
        checkout_session, checkout_id, publishable_key, checkout_page
    )
    _inspect_init(bootstrap_payload, f"{CHECKOUT_COUNTRY} Bootstrap", require_zero=False, log=log)

    _check_running(stop_event)
    _update_promotion(promotion_session, token, checkout_id, checkout)
    init_payload, stripe_js_id = _stripe_init(
        provider_session, checkout_id, publishable_key, checkout_page
    )
    amount = _inspect_init(
        init_payload,
        f"{PROMOTION_COUNTRY} 更新后 {PROVIDER_COUNTRY}",
        require_zero=True,
        log=log,
    )

    billing = _billing(token)
    _check_running(stop_event)
    _update_taxes(provider_session, token, checkout_id, checkout, billing)
    _update_tax_region(
        provider_session,
        checkout_id,
        publishable_key,
        checkout_page,
        stripe_js_id,
        billing,
    )
    init_payload, stripe_js_id = _stripe_init(
        provider_session, checkout_id, publishable_key, checkout_page
    )
    amount = _inspect_init(
        init_payload, f"{PROVIDER_COUNTRY} 税务同步", require_zero=True, log=log
    )

    _check_running(stop_event)
    pre_confirm = provider_session.post(
        f"https://api.stripe.com/v1/payment_pages/{checkout_id}/pre_confirm",
        data={
            "eid": str(uuid.uuid4()),
            "payment_method_type": "kakao_pay",
            "key": publishable_key,
            "_stripe_version": STRIPE_VERSION,
        },
        headers=_stripe_headers(publishable_key, checkout_page),
        timeout=TIMEOUT,
    )
    if pre_confirm.status_code != 200:
        raise KakaoError(f"pre_confirm failed {pre_confirm.status_code}: {_response_error(pre_confirm)}")

    client_session_id = str(uuid.uuid4())
    guid = f"{uuid.uuid4()}{os.urandom(3).hex()}"
    muid = f"{uuid.uuid4()}{os.urandom(3).hex()}"
    sid = f"{uuid.uuid4()}{os.urandom(3).hex()}"
    payment_method_body = {
        "type": "kakao_pay",
        "billing_details[name]": billing["name"],
        "billing_details[email]": billing["email"],
        "billing_details[address][country]": PROVIDER_COUNTRY,
        "billing_details[address][line1]": billing["line1"],
        "billing_details[address][line2]": billing["line2"],
        "billing_details[address][city]": billing["city"],
        "billing_details[address][postal_code]": billing["postal_code"],
        "billing_details[address][state]": billing["state"],
        "guid": guid,
        "muid": muid,
        "sid": sid,
        "_stripe_version": STRIPE_VERSION,
        "key": publishable_key,
        "payment_user_agent": STRIPE_PAYMENT_UA,
        "client_attribution_metadata[client_session_id]": client_session_id,
        "client_attribution_metadata[checkout_session_id]": checkout_id,
        "client_attribution_metadata[merchant_integration_source]": "checkout",
        "client_attribution_metadata[merchant_integration_version]": "custom_checkout",
        "client_attribution_metadata[payment_method_selection_flow]": "merchant_specified",
    }
    config_id = str(init_payload.get("config_id") or "")
    if config_id:
        payment_method_body["client_attribution_metadata[checkout_config_id]"] = config_id
    payment_method_response = provider_session.post(
        "https://api.stripe.com/v1/payment_methods",
        data=payment_method_body,
        headers=_stripe_headers(publishable_key, checkout_page),
        timeout=TIMEOUT,
    )
    if payment_method_response.status_code != 200:
        raise KakaoError(
            f"payment method failed {payment_method_response.status_code}: "
            f"{_response_error(payment_method_response, 1000)}"
        )
    payment_method_id = str((payment_method_response.json() or {}).get("id") or "")
    if not payment_method_id.startswith("pm_"):
        raise KakaoError("payment method 响应缺少 pm_ id")

    processor_entity = str(checkout.get("processor_entity") or "openai_llc")
    success_url = (
        f"https://chatgpt.com/backend-api/payments/checkout/{processor_entity}/{checkout_id}/success?"
        f"billing_country={PROVIDER_COUNTRY}"
    )
    return_url = (
        f"https://checkout.stripe.com/c/pay/{checkout_id}?returned_from_redirect=true&ui_mode=custom&"
        f"return_url={quote(success_url, safe='')}"
    )
    confirm_body = {
        "eid": "NA",
        "payment_method": payment_method_id,
        "expected_amount": amount,
        "tax_id_collection[purchasing_as_business]": "false",
        "expected_payment_method_type": "kakao_pay",
        "return_url": return_url,
        "_stripe_version": STRIPE_VERSION,
        "guid": guid,
        "muid": muid,
        "sid": sid,
        "key": publishable_key,
        "version": STRIPE_RUNTIME,
        "init_checksum": str(init_payload.get("init_checksum") or ""),
        "client_attribution_metadata[client_session_id]": client_session_id,
        "client_attribution_metadata[checkout_session_id]": checkout_id,
        "client_attribution_metadata[merchant_integration_source]": "checkout",
        "client_attribution_metadata[merchant_integration_version]": "custom_checkout",
        "client_attribution_metadata[payment_method_selection_flow]": "merchant_specified",
        "link_brand": "link",
        **_elements_params(stripe_js_id, f"elements_session_{uuid.uuid4().hex[:11]}"),
    }
    if config_id:
        confirm_body["client_attribution_metadata[checkout_config_id]"] = config_id
    elements_session_id = f"elements_session_{uuid.uuid4().hex[:11]}"
    confirm_body.update(_elements_params(stripe_js_id, elements_session_id))
    confirm_response = provider_session.post(
        f"https://api.stripe.com/v1/payment_pages/{checkout_id}/confirm",
        data=confirm_body,
        headers=_stripe_headers(publishable_key, checkout_page),
        timeout=TIMEOUT,
    )
    if confirm_response.status_code != 200:
        raise KakaoError(f"confirm failed {confirm_response.status_code}: {_response_error(confirm_response, 1000)}")
    confirm_payload = confirm_response.json() or {}
    redirect = _extract_redirect(confirm_payload)
    submission = (
        confirm_payload.get("submission_attempt")
        if isinstance(confirm_payload.get("submission_attempt"), dict)
        else {}
    )

    if not redirect and (
        submission.get("state") == "requires_approval" or checkout.get("requires_manual_approval")
    ):
        last_error = ""
        approval_limit = max(1, min(APPROVE_RETRY_MAX, approve_retries))
        for index in range(1, approval_limit + 1):
            _check_running(stop_event)
            approval_response = provider_session.post(
                "https://chatgpt.com/backend-api/payments/checkout/approve",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "oai-language": "ko-KR",
                    "User-Agent": USER_AGENT,
                    "Referer": _checkout_page_url(checkout_id, checkout),
                },
                json={"checkout_session_id": checkout_id, "processor_entity": processor_entity},
                timeout=TIMEOUT,
            )
            if approval_response.status_code == 200:
                try:
                    if (approval_response.json() or {}).get("result") == "approved":
                        last_error = ""
                        break
                except (TypeError, ValueError):
                    pass
            last_error = (
                f"approve failed {approval_response.status_code}: "
                f"{_response_error(approval_response, 500)}"
            )
            if index < approval_limit:
                time.sleep(1)
        if last_error:
            raise KakaoError(last_error)

    deadline = time.time() + POLL_TIMEOUT
    poll_params = {
        "key": publishable_key,
        **_elements_params(stripe_js_id, elements_session_id),
    }
    while not redirect and time.time() < deadline:
        _check_running(stop_event)
        poll_response = provider_session.get(
            f"https://api.stripe.com/v1/payment_pages/{checkout_id}",
            params=poll_params,
            headers=_stripe_headers(publishable_key, checkout_page),
            timeout=min(8, TIMEOUT),
        )
        if poll_response.status_code == 200:
            redirect = _extract_redirect(poll_response.json() or {})
        if not redirect:
            time.sleep(1)
    if not redirect:
        raise KakaoError("redirect url timeout")

    current = redirect
    for _ in range(6):
        _check_running(stop_event)
        host = urlsplit(current).netloc.lower()
        if "nicepay" in host or "kakao" in host:
            break
        response = provider_session.get(current, allow_redirects=False, timeout=TIMEOUT)
        location = str(response.headers.get("Location") or "")
        if response.status_code not in {301, 302, 303, 307, 308} or not location:
            break
        current = urljoin(current, location)
    host = urlsplit(current).netloc.lower()
    if "nicepay" not in host and "kakao" not in host:
        raise KakaoError(f"provider redirect host unexpected: {host or 'empty'}")
    return {
        "checkout_session_id": checkout_id,
        "payment_method_id": payment_method_id,
        "stripe_redirect_url": redirect,
        "provider_redirect_url": current,
        "amount": amount,
        "link_type": "kakao",
        "payment_method": "Kakao Pay",
    }


def run_kakao_link(
    token: str,
    *,
    proxy_pool: list[str] | tuple[str, ...] = (),
    approve_retries: int = 3,
    log: LogFn = lambda _message: None,
    should_cancel: CancelFn = lambda: False,
) -> dict[str, Any]:
    """Try each configured Korean proxy seed until a Nicepay/Kakao URL is returned."""
    candidates = [str(proxy).strip() for proxy in proxy_pool if str(proxy).strip()]
    if not candidates:
        raise KakaoError("请先在管理端配置韩国 Kakao 代理 Seed")
    stop_signal = _CallbackStopSignal(should_cancel)
    last_error = ""
    for index, seed in enumerate(candidates, start=1):
        _check_running(stop_signal)
        try:
            checkout_proxy, promotion_proxy, provider_proxy = _materialized_proxy_chain(seed)
            log(
                f"[kakao] seed {index}/{len(candidates)}: "
                f"{CHECKOUT_COUNTRY} checkout -> {PROMOTION_COUNTRY} promotion -> "
                f"{PROVIDER_COUNTRY} provider"
            )
            if _preflight_enabled():
                checked: set[str] = set()
                for role, expected, proxy in (
                    ("checkout", CHECKOUT_COUNTRY, checkout_proxy),
                    ("promotion", PROMOTION_COUNTRY, promotion_proxy),
                    ("provider", PROVIDER_COUNTRY, provider_proxy),
                ):
                    if proxy in checked:
                        continue
                    checked.add(proxy)
                    actual = _exit_country(proxy)
                    if actual != expected:
                        raise KakaoError(
                            f"{role} 代理出口国家为 {actual or 'UNKNOWN'}，要求 {expected}"
                        )
                    log(f"[kakao] {role} proxy country verified: {actual}")
            return _kakao_link(
                token,
                checkout_proxy,
                promotion_proxy,
                provider_proxy,
                approve_retries=approve_retries,
                log=log,
                stop_event=stop_signal,
            )
        except Exception as exc:
            last_error = _redact_proxy_error(
                str(exc),
                seed,
                locals().get("checkout_proxy", ""),
                locals().get("promotion_proxy", ""),
                locals().get("provider_proxy", ""),
            )
            log(f"[kakao] seed {index} failed: {last_error[:300]}")
            if should_cancel():
                raise KakaoError("任务已停止") from exc
    raise KakaoError(last_error or "未获取 Kakao/Nicepay 跳转链接")


__all__ = ["KakaoError", "run_kakao_link"]
