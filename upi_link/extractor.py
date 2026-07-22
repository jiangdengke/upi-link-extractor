from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .core import UpiQrError, run_upi_qr_probe
from .credentials import Credential


LogFn = Callable[[str], None]
CancelFn = Callable[[], bool]


@dataclass(frozen=True)
class ExtractionOptions:
    proxy_pool: tuple[str, ...] = ()
    login_proxy: str | None = None
    approve_retries: int = 30
    approve_concurrency: int = 1
    proxy_from_step: int = 3


async def extract_upi_link(
    credential: Credential,
    options: ExtractionOptions,
    qr_out_path: Path,
    log: LogFn,
    should_cancel: CancelFn,
) -> dict:
    """Run the extracted UPI engine with an injected access-token login."""

    async def login_fn(*, force_fresh: bool = False, proxy: str | None = None) -> dict:
        del force_fresh, proxy
        return {
            "accessToken": credential.access_token,
            "__cookies": [],
            "user": {"email": credential.email},
        }

    qr_out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        result = await run_upi_qr_probe(
            email=credential.email,
            password="",
            secret=None,
            proxy_pool=list(options.proxy_pool),
            approve_retries=options.approve_retries,
            qr_out_path=qr_out_path,
            log=log,
            proxy_from_step=options.proxy_from_step,
            approve_concurrency=options.approve_concurrency,
            login_fn=login_fn,
            login_proxy_url=options.login_proxy,
            should_cancel=should_cancel,
        )
    except UpiQrError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 - boundary converts core failures
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    return {
        "ok": bool(result.ok or result.payment_link),
        "email": result.email,
        "amount": result.amount,
        "payment_link": result.payment_link or "",
        "qr_path": result.qr_path or "",
        "qr_source": result.qr_source or "",
        "qr_expires_at": result.qr_expires_at,
        "already_paid": result.already_paid,
        "elapsed_seconds": round(result.elapsed_seconds, 2),
        "error": result.error or "",
    }

