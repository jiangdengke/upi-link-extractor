from __future__ import annotations

import secrets
import sqlite3
import string
import threading
import time
from pathlib import Path


class CdkError(ValueError):
    pass


_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
CDK_KINDS = {"extract", "foarge"}


def normalize_code(value: str) -> str:
    return "".join(ch for ch in str(value or "").upper().strip() if ch.isalnum() or ch == "-")


class CdkStore:
    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    def generate(
        self,
        *,
        count: int,
        max_uses: int,
        expires_in_days: int = 30,
        note: str = "",
        prefix: str = "UPI",
        kind: str = "extract",
    ) -> list[dict]:
        count = max(1, min(100, int(count)))
        max_uses = max(1, min(10000, int(max_uses)))
        expires_at = int(time.time()) + expires_in_days * 86400 if expires_in_days > 0 else None
        safe_prefix = "".join(ch for ch in prefix.upper() if ch in string.ascii_uppercase + string.digits)[:12] or "UPI"
        normalized_kind = str(kind or "extract").strip().lower()
        if normalized_kind not in CDK_KINDS:
            raise CdkError("不支持的 CDK 类型")
        created_at = int(time.time())
        generated: list[dict] = []
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            while len(generated) < count:
                code = f"{safe_prefix}-{_segment()}-{_segment()}-{_segment()}"
                try:
                    conn.execute(
                        """
                        INSERT INTO cdks (
                            code, max_uses, used_uses, reserved_uses,
                            expires_at, created_at, revoked, note, kind
                        ) VALUES (?, ?, 0, 0, ?, ?, 0, ?, ?)
                        """,
                        (
                            code,
                            max_uses,
                            expires_at,
                            created_at,
                            note.strip()[:500],
                            normalized_kind,
                        ),
                    )
                except sqlite3.IntegrityError:
                    continue
                generated.append(self._row_to_dict(conn.execute("SELECT * FROM cdks WHERE code = ?", (code,)).fetchone()))
            conn.commit()
        return generated

    def verify(self, code: str) -> dict:
        normalized = normalize_code(code)
        if not normalized:
            return {"ok": False, "code": "", "message": "请填写 CDK"}
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM cdks WHERE code = ?", (normalized,)).fetchone()
        if row is None:
            return {"ok": False, "code": normalized, "message": "CDK 不存在"}
        data = self._row_to_dict(row)
        message = self._invalid_reason(data)
        data.update({"ok": not message, "message": message or "CDK 可用"})
        return data

    def reserve(self, code: str, job_id: str) -> dict:
        return self.reserve_many(code, [job_id])

    def reserve_many(self, code: str, job_ids: list[str]) -> dict:
        normalized = normalize_code(code)
        unique_job_ids = list(dict.fromkeys(str(job_id) for job_id in job_ids if job_id))
        if not unique_job_ids:
            raise CdkError("没有可预占的任务")
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM cdks WHERE code = ?", (normalized,)).fetchone()
            if row is None:
                raise CdkError("CDK 不存在")
            data = self._row_to_dict(row)
            reason = self._invalid_reason(data)
            if reason:
                raise CdkError(reason)
            if data["remaining_uses"] < len(unique_job_ids):
                raise CdkError(
                    f"CDK 可用次数不足：需要 {len(unique_job_ids)} 次，剩余 {data['remaining_uses']} 次"
                )
            now = int(time.time())
            conn.executemany(
                "INSERT INTO cdk_reservations (job_id, code, status, created_at) VALUES (?, ?, 'reserved', ?)",
                [(job_id, normalized, now) for job_id in unique_job_ids],
            )
            conn.execute(
                "UPDATE cdks SET reserved_uses = reserved_uses + ? WHERE code = ?",
                (len(unique_job_ids), normalized),
            )
            conn.commit()
        return self.verify(normalized)

    def finalize(self, job_id: str, *, success: bool) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            reservation = conn.execute(
                "SELECT code, status FROM cdk_reservations WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if reservation is None or reservation["status"] != "reserved":
                conn.rollback()
                return
            code = reservation["code"]
            if success:
                conn.execute(
                    """
                    UPDATE cdks
                    SET reserved_uses = MAX(0, reserved_uses - 1), used_uses = used_uses + 1
                    WHERE code = ?
                    """,
                    (code,),
                )
                status = "consumed"
            else:
                conn.execute(
                    "UPDATE cdks SET reserved_uses = MAX(0, reserved_uses - 1) WHERE code = ?",
                    (code,),
                )
                status = "released"
            conn.execute(
                "UPDATE cdk_reservations SET status = ?, finished_at = ? WHERE job_id = ?",
                (status, int(time.time()), job_id),
            )
            conn.commit()

    def list(self, *, limit: int = 500) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM cdks ORDER BY created_at DESC, code DESC LIMIT ?",
                (max(1, min(2000, limit)),),
            ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def set_revoked(self, code: str, revoked: bool) -> dict:
        normalized = normalize_code(code)
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                "UPDATE cdks SET revoked = ? WHERE code = ?",
                (1 if revoked else 0, normalized),
            )
            if cursor.rowcount == 0:
                raise CdkError("CDK 不存在")
            conn.commit()
        return self.verify(normalized)

    def _initialize(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS cdks (
                    code TEXT PRIMARY KEY,
                    max_uses INTEGER NOT NULL,
                    used_uses INTEGER NOT NULL DEFAULT 0,
                    reserved_uses INTEGER NOT NULL DEFAULT 0,
                    expires_at INTEGER,
                    created_at INTEGER NOT NULL,
                    revoked INTEGER NOT NULL DEFAULT 0,
                    note TEXT NOT NULL DEFAULT '',
                    kind TEXT NOT NULL DEFAULT 'extract'
                );
                CREATE TABLE IF NOT EXISTS cdk_reservations (
                    job_id TEXT PRIMARY KEY,
                    code TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    finished_at INTEGER,
                    FOREIGN KEY(code) REFERENCES cdks(code)
                );
                CREATE INDEX IF NOT EXISTS idx_cdk_reservations_code
                    ON cdk_reservations(code);
                """
            )
            columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(cdks)").fetchall()
            }
            if "kind" not in columns:
                conn.execute(
                    "ALTER TABLE cdks ADD COLUMN kind TEXT NOT NULL DEFAULT 'extract'"
                )
            conn.execute(
                "UPDATE cdks SET kind = 'extract' WHERE kind NOT IN ('extract', 'foarge')"
            )
            conn.execute("UPDATE cdks SET reserved_uses = 0")
            conn.execute(
                "UPDATE cdk_reservations SET status = 'released', finished_at = ? WHERE status = 'reserved'",
                (int(time.time()),),
            )
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @staticmethod
    def _invalid_reason(data: dict) -> str:
        if data["revoked"]:
            return "CDK 已停用"
        if data["expires_at"] and data["expires_at"] <= int(time.time()):
            return "CDK 已过期"
        if data["remaining_uses"] <= 0:
            return "CDK 次数已用完或正在被任务占用"
        return ""

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict:
        max_uses = int(row["max_uses"])
        used_uses = int(row["used_uses"])
        reserved_uses = int(row["reserved_uses"])
        return {
            "code": row["code"],
            "max_uses": max_uses,
            "used_uses": used_uses,
            "reserved_uses": reserved_uses,
            "remaining_uses": max(0, max_uses - used_uses - reserved_uses),
            "expires_at": row["expires_at"],
            "created_at": row["created_at"],
            "revoked": bool(row["revoked"]),
            "note": row["note"],
            "kind": row["kind"],
        }


def _segment(length: int = 4) -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(length))
