from __future__ import annotations

import asyncio
import json
import sqlite3
import time

import pytest

from upi_link.foarge import FoargeClientPool, FoargeError
from upi_link.foarge_pool import FoargeCdkStore, FoargePoolError


def test_legacy_single_cdk_migrates_to_available_pool(tmp_path) -> None:
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE app_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at INTEGER NOT NULL)"
        )
        conn.execute(
            "INSERT INTO app_settings (key, value, updated_at) VALUES ('foarge', ?, ?)",
            (json.dumps({"cdk": "PBK-OLD1-OLD2-OLD3"}), int(time.time())),
        )

    store = FoargeCdkStore(path)
    status = store.status()
    assert status["available_count"] == 1
    assert status["entries"][0]["masked_cdk"] == "PBK-****OLD3"
    assert "PBK-OLD1-OLD2-OLD3" not in repr(status)
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT 1 FROM app_settings WHERE key = 'foarge'"
        ).fetchone() is None


def test_pool_claims_each_cdk_once_and_persists_status(tmp_path) -> None:
    store = FoargeCdkStore(tmp_path / "pool.db")
    status = store.configure(
        "PBK-AAAA-BBBB-0001\nPBK-AAAA-BBBB-0002\nPBK-AAAA-BBBB-0001"
    )
    assert status["configured_count"] == 2
    assert status["available_count"] == 2

    first = store.claim("job-1", start_index=0)
    second = store.claim("job-2", start_index=0)
    assert first != second
    assert store.status()["reserved_count"] == 2

    store.mark_used("job-1", first)
    store.release("job-2", second)
    status = store.status()
    assert status["used_count"] == 1
    assert status["available_count"] == 1

    claimed_again = store.claim("job-3")
    assert claimed_again == second
    store.mark_used("job-3", claimed_again)
    with pytest.raises(FoargePoolError, match="无可用"):
        store.claim("job-4")


def test_restart_keeps_unresolved_reservation_for_reconciliation(tmp_path) -> None:
    path = tmp_path / "restart.db"
    store = FoargeCdkStore(path)
    store.configure("PBK-RST1-RST2-RST3")
    store.claim("job-running")

    restarted = FoargeCdkStore(path)
    status = restarted.status()
    assert status["reserved_count"] == 1
    assert status["used_count"] == 0
    assert status["available_count"] == 0


def test_client_pool_marks_failed_and_successful_codes_used(tmp_path) -> None:
    store = FoargeCdkStore(tmp_path / "client-pool.db")
    store.configure("PBK-AAAA-BBBB-0001\nPBK-AAAA-BBBB-0002")
    clients: list[FakeOneTimeClient] = []

    def factory(cdk: str):
        client = FakeOneTimeClient(cdk, fail=cdk.endswith("0001"))
        clients.append(client)
        return client

    pool = FoargeClientPool(
        lambda: store.claim("job-1"),
        lambda cdk, task_id: store.bind_task("job-1", cdk, task_id),
        lambda cdk: store.mark_used("job-1", cdk),
        lambda cdk: store.release("job-1", cdk),
        client_factory=factory,
    )

    task = asyncio.run(pool.create_task(email="owner@example.com", external_ref="order-1"))
    assert task["id"] == "task_1"
    assert len(clients) == 2
    assert store.status()["used_count"] == 1
    assert store.status()["reserved_count"] == 1
    assert store.status()["available_count"] == 0
    asyncio.run(pool.get_task("task_1"))
    assert clients[0].get_calls == 0
    assert clients[1].get_calls == 1
    pool.settle(success=True)
    assert store.status()["used_count"] == 2
    assert store.status()["reserved_count"] == 0


def test_global_queue_rejection_releases_code(tmp_path) -> None:
    store = FoargeCdkStore(tmp_path / "release.db")
    store.configure("PBK-AAAA-BBBB-0001")
    pool = FoargeClientPool(
        lambda: store.claim("job-1"),
        lambda cdk, task_id: store.bind_task("job-1", cdk, task_id),
        lambda cdk: store.mark_used("job-1", cdk),
        lambda cdk: store.release("job-1", cdk),
        client_factory=lambda cdk: FakeOneTimeClient(cdk, global_full=True),
    )
    with pytest.raises(FoargeError, match="queue full"):
        asyncio.run(pool.create_task(email="owner@example.com", external_ref="order-1"))
    assert store.status()["available_count"] == 1
    assert store.status()["used_count"] == 0


def test_failed_task_releases_code_for_reuse(tmp_path) -> None:
    store = FoargeCdkStore(tmp_path / "failed.db")
    store.configure("PBK-AAAA-BBBB-0001")
    pool = FoargeClientPool(
        lambda: store.claim("job-1"),
        lambda cdk, task_id: store.bind_task("job-1", cdk, task_id),
        lambda cdk: store.mark_used("job-1", cdk),
        lambda cdk: store.release("job-1", cdk),
        client_factory=lambda cdk: FakeOneTimeClient(cdk),
    )
    asyncio.run(pool.create_task(email="owner@example.com", external_ref="order-1"))
    assert store.status()["reserved_count"] == 1
    pool.settle(success=False)
    assert store.status()["available_count"] == 1
    assert store.claim("job-2") == "PBK-AAAA-BBBB-0001"


class FakeOneTimeClient:
    def __init__(
        self,
        cdk: str,
        *,
        fail: bool = False,
        global_full: bool = False,
    ) -> None:
        self.cdk = cdk
        self.fail = fail
        self.global_full = global_full
        self.get_calls = 0

    async def create_task(self, *, email: str, external_ref: str) -> dict:
        del email, external_ref
        if self.fail:
            raise FoargeError(
                "no uses",
                status_code=402,
                code="insufficient_uses",
            )
        if self.global_full:
            raise FoargeError(
                "queue full",
                status_code=429,
                code="global_queue_full",
            )
        return {"id": "task_1", "status": "queued"}

    async def get_task(self, task_id: str) -> dict:
        self.get_calls += 1
        return {"id": task_id, "status": "queued"}

    async def smart_release(self, task_id: str) -> None:
        del task_id
