from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .core import KakaoError, UpiQrError, run_kakao_link, run_upi_qr_probe
from .credentials import Credential

LogFn = Callable[[str], None]
CancelFn = Callable[[], bool]


@dataclass(frozen=True)
class ExtractionOptions:
    link_type: str = "upi"
    proxy_pool: tuple[str, ...] = ()
    kakao_proxy_pool: tuple[str, ...] = ()
    login_proxy: str | None = None
    approve_retries: int = 30
    approve_concurrency: int = 4
    proxy_from_step: int = 3
    max_checkout_cycles: int = 3
    relogin_block_streak: int = 10
    backend_exception_restart_threshold: int = 3
    max_checkout_restarts: int = 2


async def extract_upi_link(
    credential: Credential,
    options: ExtractionOptions,
    qr_out_path: Path,
    log: LogFn,
    should_cancel: CancelFn,
) -> dict:
    """Run the selected payment-link engine with an injected access token."""

    if options.link_type == "kakao":
        try:
            result = await asyncio.to_thread(
                run_kakao_link,
                credential.access_token,
                proxy_pool=options.kakao_proxy_pool,
                approve_retries=options.approve_retries,
                log=log,
                should_cancel=should_cancel,
            )
        except KakaoError as exc:
            return {"ok": False, "link_type": "kakao", "error": str(exc)}
        except Exception as exc:  # noqa: BLE001 - boundary converts core failures
            return {
                "ok": False,
                "link_type": "kakao",
                "error": f"{type(exc).__name__}: {exc}",
            }
        payment_link = str(result.get("provider_redirect_url") or "")
        return {
            "ok": bool(payment_link),
            "email": credential.email,
            "amount": result.get("amount"),
            "payment_link": payment_link,
            "stripe_redirect_url": result.get("stripe_redirect_url") or "",
            "checkout_session_id": result.get("checkout_session_id") or "",
            "payment_method_id": result.get("payment_method_id") or "",
            "payment_method": "Kakao Pay",
            "link_type": "kakao",
            "qr_path": "",
            "error": "" if payment_link else "未获取 Kakao/Nicepay 跳转链接",
        }

    async def login_fn(*, force_fresh: bool = False, proxy: str | None = None) -> dict:
        del force_fresh, proxy
        return {
            "accessToken": credential.access_token,
            "__cookies": [],
            "user": {"email": credential.email},
        }

    qr_out_path.parent.mkdir(parents=True, exist_ok=True)
    result = None
    remaining_retries = options.approve_retries
    total_elapsed = 0.0
    try:
        for cycle in range(1, max(1, options.max_checkout_cycles) + 1):
            if should_cancel() or remaining_retries <= 0:
                break
            if cycle > 1:
                log(
                    f"[upi] recovery cycle {cycle}/{options.max_checkout_cycles}: "
                    f"new checkout/proxy, remaining approve budget={remaining_retries}"
                )

            result = await run_upi_qr_probe(
                email=credential.email,
                password="",
                secret=None,
                proxy_pool=list(options.proxy_pool),
                approve_retries=remaining_retries,
                qr_out_path=qr_out_path,
                log=log,
                restart_threshold=options.backend_exception_restart_threshold,
                max_restarts=options.max_checkout_restarts,
                proxy_from_step=options.proxy_from_step,
                approve_concurrency=options.approve_concurrency,
                relogin_block_streak=(
                    min(options.relogin_block_streak, remaining_retries)
                    if cycle < options.max_checkout_cycles
                    else 0
                ),
                login_fn=login_fn,
                force_fresh=cycle > 1,
                login_proxy_url=options.login_proxy,
                should_cancel=should_cancel,
            )
            total_elapsed += result.elapsed_seconds
            attempts_used = len(result.approve_attempts)
            remaining_retries = max(0, remaining_retries - attempts_used)

            if (
                result.ok
                or result.payment_link
                or not result.relogin_requested
                or remaining_retries <= 0
            ):
                break
    except UpiQrError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 - boundary converts core failures
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    if result is None:
        return {"ok": False, "error": "任务已取消"}

    return {
        "ok": bool(result.ok or result.payment_link),
        "link_type": "upi",
        "payment_method": "UPI",
        "email": result.email,
        "amount": result.amount,
        "payment_link": result.payment_link or "",
        "qr_path": result.qr_path or "",
        "qr_source": result.qr_source or "",
        "qr_expires_at": result.qr_expires_at,
        "already_paid": result.already_paid,
        "elapsed_seconds": round(total_elapsed, 2),
        "error": result.error or "",
    }
