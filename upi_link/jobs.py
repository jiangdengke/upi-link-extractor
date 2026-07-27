from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable
from uuid import uuid4

from .credentials import Credential, redact_sensitive
from .extractor import ExtractionOptions, extract_upi_link


Runner = Callable[
    [Credential, ExtractionOptions, Path, Callable[[str], None], Callable[[], bool]],
    Awaitable[dict],
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Job:
    id: str
    email: str
    link_type: str = "upi"
    owner_id: str = field(default="", repr=False)
    status: str = "queued"
    created_at: str = field(default_factory=_now)
    started_at: str | None = None
    finished_at: str | None = None
    logs: list[str] = field(default_factory=list)
    result: dict | None = None
    payment: dict | None = None
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    on_complete: Callable[["Job"], None] | None = field(default=None, repr=False)
    completion_notified: bool = field(default=False, repr=False)

    def snapshot(self) -> dict:
        result = dict(self.result or {})
        result.pop("qr_path", None)
        if self.result and self.result.get("qr_path"):
            result["qr_url"] = f"/api/jobs/{self.id}/qr"
        return {
            "id": self.id,
            "email": self.email,
            "link_type": self.link_type,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "logs": list(self.logs),
            "result": result or None,
            "payment": dict(self.payment) if self.payment else None,
        }


class JobManager:
    def __init__(
        self,
        qr_root: Path,
        *,
        max_concurrency: int = 1,
        runner: Runner = extract_upi_link,
    ) -> None:
        self.qr_root = qr_root.resolve()
        self.qr_root.mkdir(parents=True, exist_ok=True)
        self._jobs: dict[str, Job] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._semaphore = asyncio.Semaphore(max(1, max_concurrency))
        self._runner = runner

    def create(
        self,
        credential: Credential,
        options: ExtractionOptions,
        *,
        owner_id: str = "",
        job_id: str | None = None,
        on_complete: Callable[[Job], None] | None = None,
        runner: Runner | None = None,
        use_concurrency_slot: bool = True,
    ) -> dict:
        self._trim_completed()
        job = Job(
            id=job_id or uuid4().hex,
            email=credential.email,
            link_type=options.link_type,
            owner_id=owner_id,
            on_complete=on_complete,
        )
        self._jobs[job.id] = job
        task = asyncio.create_task(
            self._run(
                job,
                credential,
                options,
                runner=runner or self._runner,
                use_concurrency_slot=use_concurrency_slot,
            )
        )
        self._tasks[job.id] = task
        task.add_done_callback(lambda _task, job_id=job.id: self._tasks.pop(job_id, None))
        return job.snapshot()

    def get(self, job_id: str, *, owner_id: str | None = None) -> Job | None:
        job = self._jobs.get(job_id)
        if job is None or (owner_id is not None and job.owner_id != owner_id):
            return None
        return job

    def list(self, *, owner_id: str | None = None) -> list[dict]:
        visible = (
            self._jobs.values()
            if owner_id is None
            else (job for job in self._jobs.values() if job.owner_id == owner_id)
        )
        jobs = sorted(visible, key=lambda item: item.created_at, reverse=True)
        return [job.snapshot() for job in jobs]

    def cancel(self, job_id: str, *, owner_id: str | None = None) -> dict | None:
        job = self.get(job_id, owner_id=owner_id)
        if job is None:
            return None
        job.cancel_event.set()
        if job.status == "queued":
            task = self._tasks.get(job_id)
            if task:
                task.cancel()
            job.status = "cancelled"
            job.finished_at = _now()
            job.logs.append("任务已在队列中取消")
            self._notify_completion(job)
        return job.snapshot()

    def qr_path(self, job_id: str, *, owner_id: str | None = None) -> Path | None:
        job = self.get(job_id, owner_id=owner_id)
        raw = str((job.result or {}).get("qr_path") or "") if job else ""
        if not raw:
            return None
        path = Path(raw).resolve()
        if path != self.qr_root and self.qr_root not in path.parents:
            return None
        return path if path.is_file() else None

    def update_progress(
        self,
        job_id: str,
        *,
        payment: dict | None = None,
        result: dict | None = None,
    ) -> dict | None:
        job = self._jobs.get(job_id)
        if job is None:
            return None
        if payment is not None:
            job.payment = dict(payment)
        if result is not None:
            merged = dict(job.result or {})
            previous_link = str(merged.get("payment_link") or "")
            merged.update(result)
            current_link = str(merged.get("payment_link") or "")
            if current_link and current_link != previous_link:
                merged["generated_at"] = _now()
            job.result = merged
        return job.snapshot()

    async def run_extraction(
        self,
        credential: Credential,
        options: ExtractionOptions,
        qr_path: Path,
        log: Callable[[str], None],
        should_cancel: Callable[[], bool],
    ) -> dict:
        while not should_cancel():
            try:
                await asyncio.wait_for(self._semaphore.acquire(), timeout=0.5)
                break
            except asyncio.TimeoutError:
                continue
        else:
            return {"ok": False, "error": "任务已取消"}
        try:
            if should_cancel():
                return {"ok": False, "error": "任务已取消"}
            return await self._runner(
                credential,
                options,
                qr_path,
                log,
                should_cancel,
            )
        finally:
            self._semaphore.release()

    async def _run(
        self,
        job: Job,
        credential: Credential,
        options: ExtractionOptions,
        *,
        runner: Runner,
        use_concurrency_slot: bool,
    ) -> None:
        try:
            async def execute() -> dict | None:
                if job.cancel_event.is_set():
                    return None
                job.status = "running"
                job.started_at = _now()

                def log(message: str) -> None:
                    safe = redact_sensitive(message, credential.access_token)
                    job.logs.append(safe)
                    if len(job.logs) > 500:
                        del job.logs[:-500]

                return await runner(
                    credential,
                    options,
                    self.qr_root / f"{job.id}.png",
                    log,
                    job.cancel_event.is_set,
                )

            if use_concurrency_slot:
                async with self._semaphore:
                    result = await execute()
            else:
                result = await execute()
            if result is not None:
                if result.get("payment_link") and not result.get("generated_at"):
                    result["generated_at"] = _now()
                job.result = result
                if job.cancel_event.is_set():
                    job.status = "cancelled"
                else:
                    job.status = "success" if result.get("ok") else "failed"
        except asyncio.CancelledError:
            job.status = "cancelled"
        except Exception as exc:  # noqa: BLE001 - task boundary
            job.status = "failed"
            job.result = {"ok": False, "error": redact_sensitive(str(exc), credential.access_token)}
        finally:
            if job.status in {"success", "failed", "cancelled"}:
                job.finished_at = _now()
                self._notify_completion(job)

    @staticmethod
    def _notify_completion(job: Job) -> None:
        if job.completion_notified or job.on_complete is None:
            return
        job.completion_notified = True
        try:
            job.on_complete(job)
        except Exception as exc:  # noqa: BLE001 - accounting callback boundary
            job.logs.append(f"[CDK] 结算失败: {type(exc).__name__}: {exc}")

    def _trim_completed(self, keep: int = 100) -> None:
        completed = [
            job for job in self._jobs.values() if job.status in {"success", "failed", "cancelled"}
        ]
        completed.sort(key=lambda item: item.finished_at or item.created_at, reverse=True)
        for job in completed[keep:]:
            self._jobs.pop(job.id, None)
