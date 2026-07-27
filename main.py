from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
import secrets
from pathlib import Path
from uuid import uuid4

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from upi_link import __version__
from upi_link.auth import AdminAuth, LoginRateLimiter
from upi_link.cdk import CdkError, CdkStore, normalize_code
from upi_link.credentials import Credential, CredentialError, parse_credential
from upi_link.extractor import ExtractionOptions
from upi_link.foarge import (
    FoargeClient,
    FoargeClientPool,
    FoargeError,
    run_foarge_payment,
)
from upi_link.foarge_pool import FoargeCdkStore, FoargePoolError
from upi_link.jobs import Job, JobManager, Runner
from upi_link.schemas import (
    AdminFoargeSettingsRequest,
    AdminLoginRequest,
    AdminSettingsRequest,
    CdkRevokeRequest,
    CdkVerifyRequest,
    CompatibilityExtractRequest,
    CreateBatchJobRequest,
    CreateCdkRequest,
    CreateJobRequest,
)
from upi_link.settings import SettingsStore

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
RUNTIME_DIR = Path(os.getenv("UPI_RUNTIME_DIR", str(BASE_DIR / "runtime"))).resolve()
ADMIN_COOKIE = "upi_admin_session"
CLIENT_COOKIE = "upi_client_session"
_CLIENT_ID_RE = re.compile(r"^[a-f0-9]{64}$")
_JOB_ID_RE = re.compile(r"^[a-f0-9]{32}$")
_COMPAT_SIGNING_SECRET = (
    os.getenv("UPI_SESSION_SECRET", "").encode("utf-8") or secrets.token_bytes(32)
)


def _max_concurrency() -> int:
    try:
        return max(1, min(20, int(os.getenv("UPI_MAX_CONCURRENCY", "1"))))
    except ValueError:
        return 1


def _cookie_secure() -> bool:
    return os.getenv("UPI_COOKIE_SECURE", "0").strip().lower() in {"1", "true", "yes", "on"}


app = FastAPI(
    title="UPI Link Extractor",
    version=__version__,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
jobs = JobManager(RUNTIME_DIR / "qr", max_concurrency=_max_concurrency())
DB_PATH = RUNTIME_DIR / "data" / "upi.db"
cdks = CdkStore(DB_PATH)
settings = SettingsStore(DB_PATH)
foarge_cdks = FoargeCdkStore(DB_PATH)
admin_auth = AdminAuth(
    os.getenv("UPI_ADMIN_PASSWORD", ""),
    os.getenv("UPI_SESSION_SECRET", ""),
)
login_limiter = LoginRateLimiter()


def _set_client_cookie(response: Response, client_id: str) -> None:
    response.set_cookie(
        CLIENT_COOKIE,
        client_id,
        max_age=365 * 86400,
        httponly=True,
        secure=_cookie_secure(),
        samesite="lax",
        path="/",
    )


def _ensure_client_id(request: Request, response: Response) -> str:
    client_id = str(request.cookies.get(CLIENT_COOKIE) or "")
    if not _CLIENT_ID_RE.fullmatch(client_id):
        client_id = secrets.token_hex(32)
        _set_client_cookie(response, client_id)
    return client_id


def _existing_client_id(request: Request) -> str:
    client_id = str(request.cookies.get(CLIENT_COOKIE) or "")
    if not _CLIENT_ID_RE.fullmatch(client_id):
        raise HTTPException(404, "任务不存在")
    return client_id


def _require_admin(request: Request) -> None:
    if not admin_auth.enabled:
        raise HTTPException(503, "管理员密码尚未配置")
    if not admin_auth.verify(str(request.cookies.get(ADMIN_COOKIE) or "")):
        raise HTTPException(401, "管理员登录已失效")


def _options(link_type: str = "upi") -> ExtractionOptions:
    config = settings.get()
    return ExtractionOptions(
        link_type=link_type,
        proxy_pool=tuple(config["proxy_pool"]),
        kakao_proxy_pool=tuple(config["kakao_proxy_pool"]),
        login_proxy=config["login_proxy"] or None,
        approve_retries=config["approve_retries"],
        approve_concurrency=config["approve_concurrency"],
        proxy_from_step=config["proxy_from_step"],
    )


def _settle_cdk(job: Job) -> None:
    success = bool(
        job.status == "success"
        and job.result
        and job.result.get("payment_link")
    )
    cdks.finalize(job.id, success=success)


def _public_cdk_status(data: dict) -> dict:
    allowed = (
        "ok",
        "code",
        "max_uses",
        "used_uses",
        "reserved_uses",
        "remaining_uses",
        "expires_at",
        "revoked",
        "message",
        "kind",
    )
    return {key: data.get(key) for key in allowed if key in data}


def _compat_owner_id(cdk: str) -> str:
    normalized = normalize_code(cdk)
    return hashlib.sha256(f"registration-compat:{normalized}".encode()).hexdigest()


def _compat_qr_access(job_id: str) -> str:
    return hmac.new(
        _COMPAT_SIGNING_SECRET,
        f"registration-compat-qr:{job_id}".encode(),
        hashlib.sha256,
    ).hexdigest()


def _compat_qr_path(job_id: str) -> Path | None:
    if not _JOB_ID_RE.fullmatch(job_id):
        return None
    path = (jobs.qr_root / f"{job_id}.png").resolve()
    if path.parent != jobs.qr_root:
        return None
    return path if path.is_file() else None


def _compat_result(
    snapshot: dict,
    *,
    request: Request,
    cdk: str,
) -> dict:
    raw = dict(snapshot.get("result") or {})
    link_type = str(raw.get("link_type") or snapshot.get("link_type") or "upi")
    job_id = str(snapshot.get("id") or "")
    payment_link = str(raw.get("payment_link") or "")
    qr_url = ""
    if raw.get("qr_url") and _JOB_ID_RE.fullmatch(job_id):
        qr_url = (
            f"{request.url_for('get_qr', job_id=job_id)}"
            f"?access={_compat_qr_access(job_id)}"
        )
    cdk_status = cdks.verify(cdk)
    return {
        "long_url": payment_link,
        "copy_paste": payment_link,
        "image_url_png": qr_url,
        "image_url_svg": "",
        "payment_method": "Kakao Pay" if link_type == "kakao" else "UPI",
        "payment_link_type": link_type,
        "expires_at": raw.get("qr_expires_at") or raw.get("expires_at"),
        "cdk_remaining": cdk_status.get("remaining_uses"),
        "email": raw.get("email") or snapshot.get("email"),
        "amount": raw.get("amount"),
        "already_paid": bool(raw.get("already_paid")),
        "elapsed_seconds": raw.get("elapsed_seconds"),
    }


def _sse_event(event: str, payload: dict) -> str:
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event}\ndata: {data}\n\n"


def _make_foarge_runner(job_id: str) -> Runner:
    start_index = int(job_id[:8], 16)
    client = FoargeClientPool(
        lambda: foarge_cdks.claim(job_id, start_index=start_index),
        lambda cdk, task_id: foarge_cdks.bind_task(job_id, cdk, task_id),
        lambda cdk: foarge_cdks.mark_used(job_id, cdk),
        lambda cdk: foarge_cdks.release(job_id, cdk),
    )

    async def payment_runner(
        credential,
        options,
        qr_path,
        log,
        should_cancel,
    ) -> dict:
        def publish_progress(state: dict) -> None:
            jobs.update_progress(job_id, payment=state)

        def publish_link(result: dict) -> None:
            jobs.update_progress(job_id, result=result)
            cdks.finalize(job_id, success=True)

        return await run_foarge_payment(
            credential,
            options,
            qr_path,
            log,
            should_cancel,
            client=client,
            external_ref=f"upi-{job_id}",
            extract=jobs.run_extraction,
            on_progress=publish_progress,
            on_link=publish_link,
        )

    return payment_runner


def _launch_jobs(
    credentials: list[Credential],
    *,
    cdk: str,
    options: ExtractionOptions,
    owner_id: str,
) -> list[dict]:
    cdk_status = cdks.verify(cdk)
    if not cdk_status.get("ok"):
        raise HTTPException(403, str(cdk_status.get("message") or "CDK 不可用"))
    if cdk_status.get("kind") == "foarge":
        if options.link_type != "upi":
            raise HTTPException(400, "支付型 CDK 当前只用于 UPI 提链")
        available = foarge_cdks.status()["available_count"]
        if available < len(credentials):
            raise HTTPException(
                503,
                f"支付服务可用一次性 CDK 不足：需要 {len(credentials)} 个，剩余 {available} 个",
            )

    job_ids = [uuid4().hex for _ in credentials]
    try:
        reservation = cdks.reserve_many(cdk, job_ids)
    except CdkError as exc:
        raise HTTPException(403, str(exc)) from exc

    snapshots: list[dict] = []
    created_ids: set[str] = set()
    try:
        for job_id, credential in zip(job_ids, credentials):
            runner = None
            use_concurrency_slot = True
            if reservation.get("kind") == "foarge":
                runner = _make_foarge_runner(job_id)
                use_concurrency_slot = False
            snapshots.append(
                jobs.create(
                    credential,
                    options,
                    owner_id=owner_id,
                    job_id=job_id,
                    on_complete=_settle_cdk,
                    runner=runner,
                    use_concurrency_slot=use_concurrency_slot,
                )
            )
            created_ids.add(job_id)
    except Exception:
        for job_id in job_ids:
            if job_id not in created_ids:
                cdks.finalize(job_id, success=False)
        raise
    return snapshots


@app.get("/", include_in_schema=False)
def index(request: Request) -> FileResponse:
    response = FileResponse(STATIC_DIR / "index.html")
    _ensure_client_id(request, response)
    return response


@app.get("/admin", include_in_schema=False)
def admin_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "admin.html")


@app.get("/api/health")
def health() -> dict:
    return {
        "ok": True,
        "version": __version__,
        "cdk_required": True,
    }


@app.post("/api/cdk/verify")
def verify_cdk(body: CdkVerifyRequest) -> dict:
    return _public_cdk_status(cdks.verify(body.code))


@app.get("/api/cdk")
def compatibility_verify_cdk(
    code: str = Query(default="", max_length=64),
) -> dict:
    """Compatibility endpoint used by turb-gpt-free-register."""

    return _public_cdk_status(cdks.verify(code))


@app.post("/api/extract", status_code=202)
async def compatibility_create_job(
    body: CompatibilityExtractRequest,
) -> JSONResponse:
    """Create an UPI job using the registration project's legacy request shape."""

    link_type = body.link_type.strip().lower()
    if link_type not in {"upi", "kakao"}:
        return JSONResponse(
            status_code=400,
            content={"error": "当前兼容接口仅支持 UPI 或 Kakao 提链"},
        )
    try:
        credential = parse_credential(
            body.token.get_secret_value(),
            body.email,
            require_email=link_type == "upi",
        )
    except CredentialError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})
    try:
        snapshot = _launch_jobs(
            [credential],
            cdk=body.cdk,
            options=_options(link_type),
            owner_id=_compat_owner_id(body.cdk),
        )[0]
    except HTTPException as exc:
        detail = (
            exc.detail
            if isinstance(exc.detail, str)
            else json.dumps(exc.detail, ensure_ascii=False)
        )
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": detail},
            headers=exc.headers,
        )
    cdk_status = cdks.verify(body.cdk)
    return JSONResponse(
        status_code=202,
        content={
            "job_id": snapshot["id"],
            "status": snapshot["status"],
            "link_type": link_type,
            "cdk_remaining": cdk_status.get("remaining_uses"),
        },
    )


@app.get("/api/jobs")
def list_jobs(request: Request, response: Response) -> dict:
    owner_id = _ensure_client_id(request, response)
    return {"jobs": jobs.list(owner_id=owner_id)}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str, request: Request) -> dict:
    job = jobs.get(job_id, owner_id=_existing_client_id(request))
    if job is None:
        raise HTTPException(404, "任务不存在")
    return job.snapshot()


@app.get("/api/jobs/{job_id}/events")
async def compatibility_job_events(
    job_id: str,
    request: Request,
    cdk: str = Query(min_length=1, max_length=64),
) -> StreamingResponse:
    """Stream legacy log/result/error events for a compatibility job."""

    job = jobs.get(job_id, owner_id=_compat_owner_id(cdk))
    if job is None:
        raise HTTPException(404, "任务不存在或 CDK 不匹配")

    async def stream():
        log_index = 0
        heartbeat_ticks = 0
        payment_status = ""
        while True:
            if await request.is_disconnected():
                return
            snapshot = job.snapshot()
            logs = snapshot.get("logs") or []
            for message in logs[log_index:]:
                yield _sse_event("log", {"message": str(message)[:1000]})
            log_index = len(logs)

            payment = snapshot.get("payment") or {}
            current_payment_status = str(payment.get("status") or "")
            if current_payment_status and current_payment_status != payment_status:
                payment_status = current_payment_status
                message = str(
                    payment.get("message") or f"支付状态：{current_payment_status}"
                )
                yield _sse_event("log", {"message": message[:1000]})

            status = str(snapshot.get("status") or "")
            if status == "success":
                yield _sse_event(
                    "result",
                    {
                        "result": _compat_result(
                            snapshot,
                            request=request,
                            cdk=cdk,
                        )
                    },
                )
                yield _sse_event("done", {"status": status})
                return
            if status in {"failed", "cancelled"}:
                result = snapshot.get("result") or {}
                message = str(
                    result.get("error")
                    or (logs[-1] if logs else "")
                    or ("任务已取消" if status == "cancelled" else "提链任务失败")
                )
                yield _sse_event(
                    "error",
                    {"error": message[:1000], "status": status},
                )
                yield _sse_event("done", {"status": status})
                return

            heartbeat_ticks += 1
            if heartbeat_ticks >= 20:
                heartbeat_ticks = 0
                yield ": keepalive\n\n"
            await asyncio.sleep(0.5)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/jobs", status_code=202)
async def create_job(
    body: CreateJobRequest,
    request: Request,
    response: Response,
) -> dict:
    if not body.authorized:
        raise HTTPException(400, "请确认该凭证属于你本人或已获得明确授权")
    try:
        credential = parse_credential(
            body.credential.get_secret_value(),
            body.email,
            require_email=body.link_type == "upi",
        )
    except CredentialError as exc:
        raise HTTPException(400, str(exc)) from exc
    owner_id = _ensure_client_id(request, response)
    return _launch_jobs(
        [credential],
        cdk=body.cdk,
        options=_options(body.link_type),
        owner_id=owner_id,
    )[0]


@app.post("/api/jobs/batch", status_code=202)
async def create_batch_jobs(
    body: CreateBatchJobRequest,
    request: Request,
    response: Response,
) -> dict:
    if not body.authorized:
        raise HTTPException(400, "请确认所有凭证均属于你本人或已获得明确授权")
    credentials: list[Credential] = []
    seen_tokens: set[str] = set()
    for index_number, item in enumerate(body.items, start=1):
        try:
            credential = parse_credential(
                item.credential.get_secret_value(),
                item.email,
                require_email=body.link_type == "upi",
            )
        except CredentialError as exc:
            raise HTTPException(400, f"第 {index_number} 项：{exc}") from exc
        if credential.access_token in seen_tokens:
            raise HTTPException(400, f"第 {index_number} 项与前面的 Access Token 重复")
        seen_tokens.add(credential.access_token)
        credentials.append(credential)
    owner_id = _ensure_client_id(request, response)
    snapshots = _launch_jobs(
        credentials,
        cdk=body.cdk,
        options=_options(body.link_type),
        owner_id=owner_id,
    )
    return {
        "jobs": snapshots,
        "count": len(snapshots),
        "cdk": _public_cdk_status(cdks.verify(body.cdk)),
    }


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str, request: Request) -> dict:
    snapshot = jobs.cancel(job_id, owner_id=_existing_client_id(request))
    if snapshot is None:
        raise HTTPException(404, "任务不存在")
    return snapshot


@app.get("/api/jobs/{job_id}/qr", include_in_schema=False)
def get_qr(job_id: str, request: Request, access: str = "") -> FileResponse:
    if access and hmac.compare_digest(access, _compat_qr_access(job_id)):
        path = _compat_qr_path(job_id)
    else:
        path = jobs.qr_path(job_id, owner_id=_existing_client_id(request))
    if path is None:
        raise HTTPException(404, "二维码尚未生成")
    media_type = "image/svg+xml" if path.suffix.lower() == ".svg" else "image/png"
    return FileResponse(path, media_type=media_type)


@app.get("/api/admin/session")
def admin_session(request: Request) -> dict:
    return {
        "authenticated": admin_auth.enabled
        and admin_auth.verify(str(request.cookies.get(ADMIN_COOKIE) or "")),
        "configured": admin_auth.enabled,
    }


@app.post("/api/admin/login")
def admin_login(body: AdminLoginRequest, request: Request, response: Response) -> dict:
    if not admin_auth.enabled:
        raise HTTPException(503, "请先配置 UPI_ADMIN_PASSWORD")
    key = request.client.host if request.client else "unknown"
    if not login_limiter.allowed(key):
        retry_after = login_limiter.retry_after(key)
        raise HTTPException(
            429,
            f"登录失败次数过多，请在 {retry_after} 秒后重试",
            headers={"Retry-After": str(retry_after)},
        )
    if not admin_auth.verify_password(body.password.get_secret_value()):
        login_limiter.record_failure(key)
        raise HTTPException(401, "密码错误")
    login_limiter.clear(key)
    response.set_cookie(
        ADMIN_COOKIE,
        admin_auth.issue(),
        max_age=admin_auth.ttl_seconds,
        httponly=True,
        secure=_cookie_secure(),
        samesite="strict",
        path="/",
    )
    return {"ok": True}


@app.post("/api/admin/logout")
def admin_logout(response: Response) -> dict:
    response.delete_cookie(ADMIN_COOKIE, path="/")
    return {"ok": True}


@app.get("/api/admin/settings")
def admin_get_settings(request: Request) -> dict:
    _require_admin(request)
    return settings.get()


@app.put("/api/admin/settings")
def admin_update_settings(body: AdminSettingsRequest, request: Request) -> dict:
    _require_admin(request)
    try:
        return settings.update(
            proxy_pool=body.proxy_pool,
            kakao_proxy_pool=body.kakao_proxy_pool,
            login_proxy=body.login_proxy,
            approve_retries=body.approve_retries,
            approve_concurrency=body.approve_concurrency,
            proxy_from_step=body.proxy_from_step,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/admin/foarge")
def admin_get_foarge(request: Request) -> dict:
    _require_admin(request)
    return foarge_cdks.status()


@app.put("/api/admin/foarge")
def admin_update_foarge(body: AdminFoargeSettingsRequest, request: Request) -> dict:
    _require_admin(request)
    try:
        raw_cdks = (
            body.cdks.get_secret_value()
            if body.cdks
            else body.cdk.get_secret_value() if body.cdk else ""
        )
        return foarge_cdks.configure(
            raw_cdks,
            clear=body.clear,
        )
    except FoargePoolError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/admin/foarge/check")
async def admin_check_foarge(request: Request) -> dict:
    _require_admin(request)
    entries = foarge_cdks.entries_for_check()
    if not entries:
        raise HTTPException(400, "请先配置 Foarge PBK CDK")
    semaphore = asyncio.Semaphore(5)

    async def check_entry(entry: dict) -> dict:
        item = {
            "masked_cdk": entry["masked_cdk"],
            "status": entry["status"],
        }
        if entry["status"] == "available":
            try:
                async with semaphore:
                    item.update(await FoargeClient(entry["code"]).check_cdk())
            except FoargeError as exc:
                item.update({"ok": False, "error": str(exc)})
        elif entry["status"] == "reserved" and entry.get("claimed_by"):
            client = FoargeClient(entry["code"])
            try:
                async with semaphore:
                    if entry.get("upstream_task_id"):
                        task = await client.get_task(entry["upstream_task_id"])
                    else:
                        task = await client.find_task(
                            external_ref=f"upi-{entry['claimed_by']}"
                        )
                if task is None:
                    foarge_cdks.release(entry["claimed_by"], entry["code"])
                    item.update({"status": "available", "message": "未发现上游任务，已释放"})
                else:
                    task_id = str(task.get("id") or task.get("task_id") or "")
                    if task_id and not entry.get("upstream_task_id"):
                        foarge_cdks.bind_task(entry["claimed_by"], entry["code"], task_id)
                    upstream_status = str(task.get("status") or "unknown").lower()
                    item["upstream_status"] = upstream_status
                    if upstream_status in {"completed"}:
                        foarge_cdks.mark_used(entry["claimed_by"], entry["code"])
                        item["status"] = "used"
                    elif upstream_status in {
                        "cancelled",
                        "canceled",
                        "expired",
                        "failed",
                        "rejected",
                    }:
                        foarge_cdks.release(entry["claimed_by"], entry["code"])
                        item["status"] = "available"
            except FoargePoolError:
                item["status"] = foarge_cdks.entry_status(entry["code"]) or item["status"]
            except FoargeError as exc:
                item.update({"ok": False, "error": str(exc)})
        return item

    items = await asyncio.gather(*(check_entry(entry) for entry in entries))
    return {"ok": all(item.get("ok", True) for item in items), "items": items}


@app.get("/api/admin/cdks")
def admin_list_cdks(request: Request) -> dict:
    _require_admin(request)
    return {"items": cdks.list()}


@app.post("/api/admin/cdks")
def admin_create_cdks(body: CreateCdkRequest, request: Request) -> dict:
    _require_admin(request)
    if body.kind == "foarge" and foarge_cdks.status()["available_count"] <= 0:
        raise HTTPException(400, "请先添加可用的 Foarge 一次性 PBK CDK")
    items = cdks.generate(
        count=body.count,
        max_uses=body.max_uses,
        expires_in_days=body.expires_in_days,
        note=body.note,
        prefix=body.prefix,
        kind=body.kind,
    )
    return {"items": items, "count": len(items)}


@app.post("/api/admin/cdks/{code}/revoke")
def admin_revoke_cdk(code: str, body: CdkRevokeRequest, request: Request) -> dict:
    _require_admin(request)
    try:
        return cdks.set_revoked(code, body.revoked)
    except CdkError as exc:
        raise HTTPException(404, str(exc)) from exc


if __name__ == "__main__":
    uvicorn.run("main:app", host="127.0.0.1", port=15336, reload=False)
