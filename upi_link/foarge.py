from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import quote

from .credentials import Credential, redact_sensitive
from .extractor import ExtractionOptions


FOARGE_API_BASE = "https://foarge.com/api/publisher/v1"
READY_STATUSES = {"awaiting_checkout", "promoted"}
SUCCESS_STATUSES = {"completed"}
FAILED_STATUSES = {"cancelled", "canceled", "expired", "failed", "rejected"}
_TASK_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

LogFn = Callable[[str], None]
CancelFn = Callable[[], bool]
ProgressFn = Callable[[dict], None]
LinkFn = Callable[[dict], None]
ExtractorFn = Callable[
    [Credential, ExtractionOptions, Path, LogFn, CancelFn], Awaitable[dict]
]


class FoargeError(RuntimeError):
    def __init__(self, message: str, *, status_code: int = 0, code: str = "") -> None:
        super().__init__(message)
        self.status_code = int(status_code)
        self.code = str(code or "")

    @property
    def retryable(self) -> bool:
        return self.status_code in {0, 408, 425, 429} or self.status_code >= 500


class FoargeClient:
    def __init__(self, cdk: str, *, timeout_seconds: float = 20.0) -> None:
        self._cdk = str(cdk or "").strip().upper()
        if not self._cdk.startswith("PBK-"):
            raise ValueError("Foarge CDK 尚未配置或格式无效")
        self.timeout_seconds = max(3.0, min(60.0, float(timeout_seconds)))

    async def check_cdk(self) -> dict:
        data = await self._request("GET", "/cdk/me")
        source = data.get("cdk") if isinstance(data.get("cdk"), dict) else data
        return {
            "ok": bool(data.get("ok", True)),
            "uses_remaining": _first(source, "uses_remaining", "remaining_uses"),
            "max_uses": _first(source, "max_uses", "uses_total"),
            "allowed_payment_methods": _string_list(
                _first(source, "allowed_payment_methods", "payment_methods")
            ),
        }

    async def create_task(self, *, email: str, external_ref: str) -> dict:
        try:
            data = await self._request(
                "POST",
                "/tasks",
                json_body={
                    "payment_method": "upi",
                    "account_email": email,
                    "external_ref": external_ref,
                    "markup_cny": 0,
                },
            )
        except FoargeError as exc:
            if not exc.retryable:
                raise
            for _attempt in range(3):
                try:
                    existing = await self.find_task(external_ref=external_ref)
                except FoargeError as lookup_error:
                    if not lookup_error.retryable:
                        raise
                    existing = None
                if existing is not None:
                    return existing
                await asyncio.sleep(1)
            raise exc
        return _task_from_payload(data)

    async def find_task(self, *, external_ref: str) -> dict | None:
        data = await self._request(
            "GET",
            f"/tasks?external_ref={quote(external_ref, safe='')}&limit=10",
        )
        tasks = data.get("tasks")
        if not isinstance(tasks, list):
            return None
        for task in tasks:
            if isinstance(task, dict) and str(task.get("external_ref") or "") == external_ref:
                return task
        return None

    async def get_task(self, task_id: str) -> dict:
        return _task_from_payload(
            await self._request("GET", f"/tasks/{_safe_task_id(task_id)}")
        )

    async def submit_checkout(
        self,
        task_id: str,
        *,
        access_token: str,
        payment_link: str,
    ) -> dict:
        data = await self._request(
            "POST",
            f"/tasks/{_safe_task_id(task_id)}/submit-checkout",
            json_body={
                "checkout_data": {
                    "access_token": access_token,
                    "pay_link": payment_link,
                }
            },
        )
        return _task_from_payload(data)

    async def refresh_checkout(self, task_id: str, *, payment_link: str) -> dict:
        data = await self._request(
            "POST",
            f"/tasks/{_safe_task_id(task_id)}/refresh-checkout",
            json_body={"checkout_data": {"pay_link": payment_link}},
        )
        return _task_from_payload(data)

    async def cancel_task(self, task_id: str) -> None:
        await self._request("POST", f"/tasks/{_safe_task_id(task_id)}/cancel")

    async def smart_release(self, task_id: str) -> None:
        await self._request("POST", f"/tasks/{_safe_task_id(task_id)}/smart-release")

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict | None = None,
    ) -> dict:
        from curl_cffi.requests import AsyncSession

        url = f"{FOARGE_API_BASE}{path}"
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._cdk}",
        }
        if json_body is not None:
            headers["Content-Type"] = "application/json"
        try:
            async with AsyncSession() as session:
                response = await session.request(
                    method,
                    url,
                    headers=headers,
                    json=json_body,
                    timeout=self.timeout_seconds,
                    allow_redirects=False,
                )
        except Exception as exc:  # noqa: BLE001 - HTTP client boundary
            raise FoargeError(f"Foarge 网络请求失败：{type(exc).__name__}") from exc

        try:
            data = response.json()
        except (TypeError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        if response.status_code >= 400:
            code = str(data.get("error") or data.get("code") or "upstream_error")
            message = _error_message(data, default=f"Foarge HTTP {response.status_code}")
            message = message.replace(self._cdk, "[redacted]")
            raise FoargeError(message, status_code=response.status_code, code=code)
        return data


async def run_foarge_payment(
    credential: Credential,
    options: ExtractionOptions,
    qr_path: Path,
    log: LogFn,
    should_cancel: CancelFn,
    *,
    client: FoargeClient,
    external_ref: str,
    extract: ExtractorFn,
    on_progress: ProgressFn,
    on_link: LinkFn,
    poll_interval: float = 8.0,
    timeout_seconds: float = 3600.0,
) -> dict:
    task_id = ""
    submitted = False
    link_result: dict | None = None
    started = time.monotonic()
    next_refresh_at = 0.0
    consecutive_sync_errors = 0
    last_status = ""

    def publish(task: dict, *, message: str = "") -> str:
        nonlocal last_status
        state = public_payment_state(task, message=message)
        state["message"] = redact_sensitive(
            str(state.get("message") or ""), credential.access_token
        )
        on_progress(state)
        status = state["status"]
        if status != last_status:
            log(f"[支付] Foarge 状态：{status}")
            last_status = status
        return status

    async def release_upstream() -> None:
        if not task_id:
            return
        try:
            if submitted:
                await client.smart_release(task_id)
            else:
                await client.cancel_task(task_id)
        except FoargeError as exc:
            log(f"[支付] 上游释放失败：{exc}")

    def failed(message: str) -> dict:
        result = dict(link_result or {})
        safe_message = redact_sensitive(str(message), credential.access_token)
        result.update(
            {"ok": False, "error": safe_message, "payment_completed": False}
        )
        return result

    try:
        log("[支付] 正在创建 Foarge UPI 任务")
        task = await client.create_task(email=credential.email, external_ref=external_ref)
        task_id = _task_id(task)
        status = publish(task)

        while status not in READY_STATUSES:
            if should_cancel():
                await release_upstream()
                return failed("任务已取消")
            if status in SUCCESS_STATUSES:
                return failed("Foarge 任务在提链前已结束")
            if status in FAILED_STATUSES:
                return failed(f"Foarge 任务已结束：{status}")
            if time.monotonic() - started >= timeout_seconds:
                await release_upstream()
                return failed("等待 Foarge 可提交状态超时")
            await _sleep(poll_interval, should_cancel)
            if should_cancel():
                await release_upstream()
                return failed("任务已取消")
            try:
                task = await client.get_task(task_id)
                consecutive_sync_errors = 0
            except FoargeError as exc:
                consecutive_sync_errors += 1
                on_progress(
                    public_payment_state(
                        {"id": task_id, "status": status},
                        message=f"状态同步重试 {consecutive_sync_errors}/5",
                    )
                )
                if not exc.retryable or consecutive_sync_errors >= 5:
                    await release_upstream()
                    return failed(str(exc))
                continue
            status = publish(task)

        if should_cancel():
            await release_upstream()
            return failed("任务已取消")

        log("[支付] 上游已就绪，开始生成 UPI 长链")
        link_result = await extract(credential, options, qr_path, log, should_cancel)
        payment_link = str(link_result.get("payment_link") or "")
        if not link_result.get("ok") or not payment_link:
            await release_upstream()
            return failed(str(link_result.get("error") or "UPI 提链失败"))
        on_link(link_result)

        submitted = True
        task = await client.submit_checkout(
            task_id,
            access_token=credential.access_token,
            payment_link=payment_link,
        )
        status = publish(task, message="支付链接已提交")
        log("[支付] UPI 长链已提交，等待支付完成")

        while True:
            if should_cancel():
                await release_upstream()
                return failed("任务已取消")
            if status in SUCCESS_STATUSES:
                result = dict(link_result)
                result.update({"ok": True, "payment_completed": True})
                return result
            if status in FAILED_STATUSES:
                return failed(f"Foarge 支付未完成：{status}")
            if time.monotonic() - started >= timeout_seconds:
                await release_upstream()
                return failed("等待 Foarge 支付完成超时")

            if _needs_refresh(task) and time.monotonic() >= next_refresh_at:
                log("[支付] 上游请求刷新，正在重新生成 UPI 长链")
                refreshed = await extract(credential, options, qr_path, log, should_cancel)
                refreshed_link = str(refreshed.get("payment_link") or "")
                if not refreshed.get("ok") or not refreshed_link:
                    await release_upstream()
                    return failed(str(refreshed.get("error") or "UPI 长链刷新失败"))
                link_result = refreshed
                on_link(refreshed)
                task = await client.refresh_checkout(task_id, payment_link=refreshed_link)
                next_refresh_at = time.monotonic() + 270.0
                status = publish(task, message="支付链接已刷新")
                continue

            await _sleep(poll_interval, should_cancel)
            if should_cancel():
                await release_upstream()
                return failed("任务已取消")
            try:
                task = await client.get_task(task_id)
                consecutive_sync_errors = 0
            except FoargeError as exc:
                consecutive_sync_errors += 1
                on_progress(
                    public_payment_state(
                        {"id": task_id, "status": status},
                        message=f"状态同步重试 {consecutive_sync_errors}/5",
                    )
                )
                if not exc.retryable or consecutive_sync_errors >= 5:
                    await release_upstream()
                    return failed(str(exc))
                continue
            status = publish(task)
    except FoargeError as exc:
        await release_upstream()
        return failed(str(exc))
    except Exception as exc:  # noqa: BLE001 - payment orchestration boundary
        await release_upstream()
        return failed(f"支付流程异常：{type(exc).__name__}: {exc}")


def public_payment_state(task: dict, *, message: str = "") -> dict:
    status = str(task.get("status") or "unknown").strip().lower()
    return {
        "provider": "foarge",
        "task_id": str(task.get("id") or task.get("task_id") or "")[:128],
        "status": status,
        "queue_position": _as_int(
            _first(task, "queue_position", "position", "queue_ahead")
        ),
        "qr_needs_refresh": _as_bool(task.get("qr_needs_refresh"))
        or _as_bool(task.get("qr_expired")),
        "refresh_count": _as_int(task.get("refresh_count")),
        "message": str(message or task.get("hint") or task.get("message") or "")[:300],
        "synced_at": datetime.now(timezone.utc).isoformat(),
    }


def _task_from_payload(data: dict) -> dict:
    task = data.get("task")
    if isinstance(task, dict):
        return task
    if data.get("id") or data.get("task_id"):
        return data
    raise FoargeError("Foarge 返回的任务数据不完整")


def _task_id(task: dict) -> str:
    value = str(task.get("id") or task.get("task_id") or "")
    return _safe_task_id(value)


def _safe_task_id(value: str) -> str:
    normalized = str(value or "")
    if not _TASK_ID_RE.fullmatch(normalized):
        raise FoargeError("Foarge 返回的任务 ID 无效")
    return quote(normalized, safe="")


def _needs_refresh(task: dict) -> bool:
    status = str(task.get("status") or "").strip().lower()
    return (
        status in {"awaiting_refresh", "refresh_required"}
        or _as_bool(task.get("qr_needs_refresh"))
        or _as_bool(task.get("qr_expired"))
    )


async def _sleep(delay: float, should_cancel: CancelFn) -> None:
    remaining = max(0.0, float(delay))
    while remaining > 0 and not should_cancel():
        interval = min(0.5, remaining)
        await asyncio.sleep(interval)
        remaining -= interval
    if remaining <= 0:
        await asyncio.sleep(0)


def _error_message(data: dict, *, default: str) -> str:
    value = data.get("detail") or data.get("message") or data.get("error")
    if isinstance(value, dict):
        value = value.get("message") or value.get("error")
    return str(value or default)[:500]


def _first(data: dict, *keys: str):
    for key in keys:
        if key in data and data[key] is not None:
            return data[key]
    return None


def _as_int(value) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _as_bool(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _string_list(value) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item)[:40] for item in value if item]
