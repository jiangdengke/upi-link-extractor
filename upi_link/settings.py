from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

DEFAULT_SETTINGS = {
    "proxy_pool": [],
    "kakao_proxy_pool": [],
    "login_proxy": "",
    "approve_retries": 30,
    "approve_concurrency": 4,
    "proxy_from_step": 3,
    "updated_at": 0,
}


class SettingsStore:
    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    def get(self) -> dict:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM app_settings WHERE key = 'global'"
            ).fetchone()
        if row is None:
            return dict(DEFAULT_SETTINGS)
        try:
            saved = json.loads(row["value"])
        except (TypeError, json.JSONDecodeError):
            saved = {}
        return self._normalize(saved)

    def update(
        self,
        *,
        proxy_pool: str,
        login_proxy: str,
        approve_retries: int,
        approve_concurrency: int,
        proxy_from_step: int,
        kakao_proxy_pool: str = "",
    ) -> dict:
        proxies = [
            line.strip()
            for line in str(proxy_pool or "").replace("\r", "").split("\n")
            if line.strip()
        ]
        if len(proxies) > 100:
            raise ValueError("代理数量不能超过 100")
        kakao_proxies = [
            line.strip()
            for line in str(kakao_proxy_pool or "").replace("\r", "").split("\n")
            if line.strip()
        ]
        if len(kakao_proxies) > 100:
            raise ValueError("韩国代理数量不能超过 100")
        data = self._normalize(
            {
                "proxy_pool": proxies,
                "kakao_proxy_pool": kakao_proxies,
                "login_proxy": str(login_proxy or "").strip(),
                "approve_retries": approve_retries,
                "approve_concurrency": approve_concurrency,
                "proxy_from_step": proxy_from_step,
                "updated_at": int(time.time()),
            }
        )
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO app_settings (key, value, updated_at)
                VALUES ('global', ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (json.dumps(data, ensure_ascii=False), data["updated_at"]),
            )
            conn.commit()
        return data

    def public_status(self) -> dict:
        data = self.get()
        return {
            "proxy_count": len(data["proxy_pool"]),
            "kakao_proxy_count": len(data["kakao_proxy_pool"]),
            "has_login_proxy": bool(data["login_proxy"]),
            "approve_retries": data["approve_retries"],
            "approve_concurrency": data["approve_concurrency"],
            "proxy_from_step": data["proxy_from_step"],
            "updated_at": data["updated_at"],
        }

    def _initialize(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS app_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                )
                """
            )
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    @staticmethod
    def _normalize(value: dict) -> dict:
        proxies = value.get("proxy_pool")
        if not isinstance(proxies, list):
            proxies = []
        kakao_proxies = value.get("kakao_proxy_pool")
        if not isinstance(kakao_proxies, list):
            kakao_proxies = []
        return {
            "proxy_pool": [str(item).strip() for item in proxies if str(item).strip()][:100],
            "kakao_proxy_pool": [
                str(item).strip() for item in kakao_proxies if str(item).strip()
            ][:100],
            "login_proxy": str(value.get("login_proxy") or "").strip(),
            "approve_retries": max(1, min(60, int(value.get("approve_retries", 30)))),
            "approve_concurrency": max(1, min(20, int(value.get("approve_concurrency", 4)))),
            "proxy_from_step": max(1, min(6, int(value.get("proxy_from_step", 3)))),
            "updated_at": max(0, int(value.get("updated_at", 0))),
        }
