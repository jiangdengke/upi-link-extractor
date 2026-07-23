from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from pathlib import Path


_CDK_RE = re.compile(r"^PBK-[A-Z0-9]+(?:-[A-Z0-9]+){2,}$")
_MAX_CDKS = 50
_STATUSES = {"available", "reserved", "used"}


class FoargePoolError(ValueError):
    pass


class FoargeCdkStore:
    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    def configure(self, value: str = "", *, clear: bool = False) -> dict:
        cdks = _normalize_cdks(value)
        now = int(time.time())
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if clear:
                conn.execute("DELETE FROM foarge_cdks")
            elif cdks:
                conn.executemany(
                    """
                    INSERT OR IGNORE INTO foarge_cdks (
                        code, status, claimed_by, added_at, used_at
                    ) VALUES (?, 'available', NULL, ?, NULL)
                    """,
                    [(cdk, now) for cdk in cdks],
                )
            conn.commit()
        return self.status()

    def status(self) -> dict:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT code, status, added_at, used_at FROM foarge_cdks "
                "ORDER BY added_at, code"
            ).fetchall()
        entries = [
            {
                "masked_cdk": mask_cdk(row["code"]),
                "status": row["status"],
                "added_at": row["added_at"],
                "used_at": row["used_at"],
            }
            for row in rows
        ]
        counts = {
            status: sum(1 for item in entries if item["status"] == status)
            for status in _STATUSES
        }
        return {
            "configured": bool(entries),
            "configured_count": len(entries),
            "available_count": counts["available"],
            "reserved_count": counts["reserved"],
            "used_count": counts["used"],
            "entries": entries,
        }

    def claim(self, job_id: str, *, start_index: int = 0) -> str:
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT code FROM foarge_cdks WHERE status = 'available' "
                "ORDER BY added_at, code"
            ).fetchall()
            if not rows:
                conn.rollback()
                raise FoargePoolError("Foarge 一次性 CDK 池已无可用兑换码")
            row = rows[int(start_index) % len(rows)]
            code = row["code"]
            cursor = conn.execute(
                """
                UPDATE foarge_cdks
                SET status = 'reserved', claimed_by = ?, upstream_task_id = NULL
                WHERE code = ? AND status = 'available'
                """,
                (job_id, code),
            )
            if cursor.rowcount != 1:
                conn.rollback()
                raise FoargePoolError("Foarge CDK 领取冲突，请重试")
            conn.commit()
        return code

    def bind_task(self, job_id: str, code: str, task_id: str) -> None:
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE foarge_cdks
                SET upstream_task_id = ?
                WHERE code = ? AND status = 'reserved' AND claimed_by = ?
                """,
                (str(task_id)[:128], code, job_id),
            )
            if cursor.rowcount != 1:
                raise FoargePoolError("Foarge CDK 任务绑定状态不一致")
            conn.commit()

    def mark_used(self, job_id: str, code: str) -> None:
        self._settle(job_id, code, status="used")

    def release(self, job_id: str, code: str) -> None:
        self._settle(job_id, code, status="available")

    def entries_for_check(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT code, status, claimed_by, upstream_task_id
                FROM foarge_cdks ORDER BY added_at, code
                """
            ).fetchall()
        return [
            {
                "code": row["code"],
                "masked_cdk": mask_cdk(row["code"]),
                "status": row["status"],
                "claimed_by": row["claimed_by"],
                "upstream_task_id": row["upstream_task_id"],
            }
            for row in rows
        ]

    def entry_status(self, code: str) -> str:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT status FROM foarge_cdks WHERE code = ?",
                (code,),
            ).fetchone()
        return str(row["status"] if row else "")

    def _settle(self, job_id: str, code: str, *, status: str) -> None:
        if status not in {"available", "used"}:
            raise ValueError("无效的 Foarge CDK 结算状态")
        now = int(time.time()) if status == "used" else None
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE foarge_cdks
                SET status = ?, claimed_by = NULL, used_at = ?,
                    upstream_task_id = CASE WHEN ? = 'used' THEN upstream_task_id ELSE NULL END
                WHERE code = ? AND status = 'reserved' AND claimed_by = ?
                """,
                (status, now, status, code, job_id),
            )
            if cursor.rowcount != 1:
                raise FoargePoolError("Foarge CDK 结算状态不一致")
            conn.commit()

    def _initialize(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS foarge_cdks (
                    code TEXT PRIMARY KEY,
                    status TEXT NOT NULL DEFAULT 'available',
                    claimed_by TEXT,
                    added_at INTEGER NOT NULL,
                    used_at INTEGER,
                    upstream_task_id TEXT
                )
                """
            )
            columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(foarge_cdks)").fetchall()
            }
            if "upstream_task_id" not in columns:
                conn.execute("ALTER TABLE foarge_cdks ADD COLUMN upstream_task_id TEXT")
            self._migrate_legacy(conn)
            conn.commit()

    def _migrate_legacy(self, conn: sqlite3.Connection) -> None:
        table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'app_settings'"
        ).fetchone()
        if table is None:
            return
        row = conn.execute(
            "SELECT value FROM app_settings WHERE key = 'foarge'"
        ).fetchone()
        if row is None:
            return
        try:
            saved = json.loads(row["value"])
            raw = saved.get("cdks") if isinstance(saved.get("cdks"), list) else saved.get("cdk", "")
            cdks = _normalize_cdks(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            cdks = []
        now = int(time.time())
        conn.executemany(
            "INSERT OR IGNORE INTO foarge_cdks (code, status, added_at) VALUES (?, 'available', ?)",
            [(cdk, now) for cdk in cdks],
        )
        conn.execute("DELETE FROM app_settings WHERE key = 'foarge'")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn


def mask_cdk(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}****{value[-4:]}"


def _normalize_cdks(value: str | list | tuple | None) -> list[str]:
    if isinstance(value, str):
        raw_items = value.replace("\r", "").split("\n")
    elif isinstance(value, (list, tuple)):
        raw_items = value
    else:
        raw_items = []
    result: list[str] = []
    for line_number, item in enumerate(raw_items, start=1):
        cdk = str(item or "").strip().upper()
        if not cdk or cdk in result:
            continue
        if len(cdk) > 128 or not _CDK_RE.fullmatch(cdk):
            raise FoargePoolError(f"Foarge CDK 格式无效：第 {line_number} 行")
        result.append(cdk)
        if len(result) > _MAX_CDKS:
            raise FoargePoolError(f"Foarge CDK 数量不能超过 {_MAX_CDKS}")
    return result
