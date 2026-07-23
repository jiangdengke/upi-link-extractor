import sqlite3
import time

import pytest

from upi_link.cdk import CdkError, CdkStore


def test_cdk_reservation_success_and_release(tmp_path) -> None:
    store = CdkStore(tmp_path / "cdk.db")
    code = store.generate(count=1, max_uses=3, expires_in_days=30)[0]["code"]

    reserved = store.reserve_many(code, ["job-1", "job-2"])
    assert reserved["remaining_uses"] == 1
    assert reserved["reserved_uses"] == 2

    store.finalize("job-1", success=True)
    store.finalize("job-2", success=False)
    result = store.verify(code)
    assert result["used_uses"] == 1
    assert result["reserved_uses"] == 0
    assert result["remaining_uses"] == 2


def test_cdk_batch_reservation_is_atomic(tmp_path) -> None:
    store = CdkStore(tmp_path / "atomic.db")
    code = store.generate(count=1, max_uses=1)[0]["code"]
    with pytest.raises(CdkError, match="可用次数不足"):
        store.reserve_many(code, ["job-1", "job-2"])
    assert store.verify(code)["reserved_uses"] == 0


def test_cdk_revocation_expiry_and_restart_release(tmp_path) -> None:
    path = tmp_path / "state.db"
    store = CdkStore(path)
    code = store.generate(count=1, max_uses=1, expires_in_days=30)[0]["code"]
    store.reserve(code, "orphan-job")

    restarted = CdkStore(path)
    assert restarted.verify(code)["remaining_uses"] == 1

    restarted.set_revoked(code, True)
    assert restarted.verify(code)["ok"] is False
    restarted.set_revoked(code, False)

    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE cdks SET expires_at = ? WHERE code = ?", (int(time.time()) - 1, code))
    assert restarted.verify(code)["message"] == "CDK 已过期"


def test_cdk_kinds_and_legacy_schema_migration(tmp_path) -> None:
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE cdks (
                code TEXT PRIMARY KEY,
                max_uses INTEGER NOT NULL,
                used_uses INTEGER NOT NULL DEFAULT 0,
                reserved_uses INTEGER NOT NULL DEFAULT 0,
                expires_at INTEGER,
                created_at INTEGER NOT NULL,
                revoked INTEGER NOT NULL DEFAULT 0,
                note TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute(
            "INSERT INTO cdks (code, max_uses, created_at) VALUES (?, 1, ?)",
            ("UPI-OLD1-OLD2-OLD3", int(time.time())),
        )

    store = CdkStore(path)
    assert store.verify("UPI-OLD1-OLD2-OLD3")["kind"] == "extract"
    payment = store.generate(count=1, max_uses=1, kind="foarge")[0]
    assert payment["kind"] == "foarge"

    with pytest.raises(CdkError, match="不支持的 CDK 类型"):
        store.generate(count=1, max_uses=1, kind="unknown")
