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
    status: str = "queued"
    created_at: str = field(default_factory=_now)
    started_at: str | None = None
    finished_at: str | None = None
    logs: list[str] = field(default_factory=list)
    result: dict | None = None
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)

    def snapshot(self) -> dict:
        result = dict(self.result or {})
        result.pop("qr_path", None)
        if self.result and self.result.get("qr_path"):
            result["qr_url"] = f"/api/jobs/{self.id}/qr"
        return {
            "id": self.id,
            "email": self.email,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "logs": list(self.logs),
            "result": result or None,
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

    def create(self, credential: Credential, options: ExtractionOptions) -> dict:
        self._trim_completed()
        job = Job(id=uuid4().hex, email=credential.email)
        self._jobs[job.id] = job
        task = asyncio.create_task(self._run(job, credential, options))
        self._tasks[job.id] = task
        task.add_done_callback(lambda _task, job_id=job.id: self._tasks.pop(job_id, None))
        return job.snapshot()

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def list(self) -> list[dict]:
        jobs = sorted(self._jobs.values(), key=lambda item: item.created_at, reverse=True)
        return [job.snapshot() for job in jobs]

    def cancel(self, job_id: str) -> dict | None:
        job = self._jobs.get(job_id)
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
        return job.snapshot()

    def qr_path(self, job_id: str) -> Path | None:
        job = self._jobs.get(job_id)
        raw = str((job.result or {}).get("qr_path") or "") if job else ""
        if not raw:
            return None
        path = Path(raw).resolve()
        if path != self.qr_root and self.qr_root not in path.parents:
            return None
        return path if path.is_file() else None

    async def _run(
        self,
        job: Job,
        credential: Credential,
        options: ExtractionOptions,
    ) -> None:
        try:
            async with self._semaphore:
                if job.cancel_event.is_set():
                    return
                job.status = "running"
                job.started_at = _now()

                def log(message: str) -> None:
                    safe = redact_sensitive(message, credential.access_token)
                    job.logs.append(safe)
                    if len(job.logs) > 500:
                        del job.logs[:-500]

                result = await self._runner(
                    credential,
                    options,
                    self.qr_root / f"{job.id}.png",
                    log,
                    job.cancel_event.is_set,
                )
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

    def _trim_completed(self, keep: int = 100) -> None:
        completed = [
            job for job in self._jobs.values() if job.status in {"success", "failed", "cancelled"}
        ]
        completed.sort(key=lambda item: item.finished_at or item.created_at, reverse=True)
        for job in completed[keep:]:
            self._jobs.pop(job.id, None)

