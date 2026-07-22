from __future__ import annotations

import os
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from upi_link import __version__
from upi_link.credentials import CredentialError, parse_credential
from upi_link.extractor import ExtractionOptions
from upi_link.jobs import JobManager
from upi_link.schemas import CreateJobRequest


BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
RUNTIME_DIR = BASE_DIR / "runtime"


def _max_concurrency() -> int:
    try:
        return max(1, min(4, int(os.getenv("UPI_MAX_CONCURRENCY", "1"))))
    except ValueError:
        return 1


app = FastAPI(title="UPI Link Extractor", version=__version__)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
jobs = JobManager(RUNTIME_DIR / "qr", max_concurrency=_max_concurrency())


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "version": __version__, "max_concurrency": _max_concurrency()}


@app.get("/api/jobs")
def list_jobs() -> dict:
    return {"jobs": jobs.list()}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "任务不存在")
    return job.snapshot()


@app.post("/api/jobs", status_code=202)
async def create_job(body: CreateJobRequest) -> dict:
    if not body.authorized:
        raise HTTPException(400, "请确认该凭证属于你本人或已获得明确授权")
    try:
        credential = parse_credential(body.credential.get_secret_value(), body.email)
    except CredentialError as exc:
        raise HTTPException(400, str(exc)) from exc

    proxies = tuple(
        line.strip()
        for line in body.proxy_pool.replace("\r", "").split("\n")
        if line.strip()
    )
    if len(proxies) > 100:
        raise HTTPException(400, "代理数量不能超过 100")

    options = ExtractionOptions(
        proxy_pool=proxies,
        login_proxy=body.login_proxy.strip() or None,
        approve_retries=body.approve_retries,
        approve_concurrency=body.approve_concurrency,
        proxy_from_step=body.proxy_from_step,
    )
    return jobs.create(credential, options)


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str) -> dict:
    snapshot = jobs.cancel(job_id)
    if snapshot is None:
        raise HTTPException(404, "任务不存在")
    return snapshot


@app.get("/api/jobs/{job_id}/qr", include_in_schema=False)
def get_qr(job_id: str) -> FileResponse:
    path = jobs.qr_path(job_id)
    if path is None:
        raise HTTPException(404, "二维码尚未生成")
    media_type = "image/svg+xml" if path.suffix.lower() == ".svg" else "image/png"
    return FileResponse(path, media_type=media_type, filename=path.name)


if __name__ == "__main__":
    uvicorn.run("main:app", host="127.0.0.1", port=15336, reload=False)

