from __future__ import annotations

from datetime import datetime, timezone
import sqlite3

from app.models import SCHEMA_SQL
from app.services.concurrency import (
    acquire_pr_lock,
    can_start_new_run,
    count_running_runs,
    get_pr_lock,
    release_pr_lock,
)


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    return conn


def test_acquire_and_release_pr_lock() -> None:
    conn = _make_conn()
    now = datetime(2026, 3, 12, 10, 0, tzinfo=timezone.utc)

    acquired = acquire_pr_lock(
        conn,
        repo="acme/widgets",
        pr_number=42,
        lock_owner="worker-a",
        run_id=7,
        lock_ttl_seconds=300,
        now=now,
    )

    lock = get_pr_lock(conn, repo="acme/widgets", pr_number=42)

    assert acquired is True
    assert lock is not None
    assert lock.lock_owner == "worker-a"
    assert lock.lock_run_id == 7
    assert lock.lock_acquired_at == "2026-03-12T10:00:00Z"
    assert lock.lock_expires_at == "2026-03-12T10:05:00Z"

    released = release_pr_lock(
        conn,
        repo="acme/widgets",
        pr_number=42,
        lock_owner="worker-a",
        run_id=7,
    )

    unlocked = get_pr_lock(conn, repo="acme/widgets", pr_number=42)
    assert released is True
    assert unlocked is not None
    assert unlocked.lock_owner is None
    assert unlocked.lock_run_id is None


def test_acquire_pr_lock_blocks_other_owner_until_expired() -> None:
    conn = _make_conn()

    first = acquire_pr_lock(
        conn,
        repo="acme/widgets",
        pr_number=8,
        lock_owner="worker-a",
        now=datetime(2026, 3, 12, 10, 0, tzinfo=timezone.utc),
    )
    second = acquire_pr_lock(
        conn,
        repo="acme/widgets",
        pr_number=8,
        lock_owner="worker-b",
        now=datetime(2026, 3, 12, 10, 10, tzinfo=timezone.utc),
    )
    third = acquire_pr_lock(
        conn,
        repo="acme/widgets",
        pr_number=8,
        lock_owner="worker-b",
        now=datetime(2026, 3, 12, 10, 20, tzinfo=timezone.utc),
    )

    lock = get_pr_lock(conn, repo="acme/widgets", pr_number=8)

    assert first is True
    assert second is False
    assert third is True
    assert lock is not None
    assert lock.lock_owner == "worker-b"


def test_count_running_runs_and_limit_check() -> None:
    conn = _make_conn()
    conn.execute(
        "INSERT INTO autofix_runs (repo, pr_number, status) VALUES (?, ?, ?)",
        ("acme/widgets", 1, "running"),
    )
    conn.execute(
        "INSERT INTO autofix_runs (repo, pr_number, status) VALUES (?, ?, ?)",
        ("acme/widgets", 2, "queued"),
    )
    conn.execute(
        "INSERT INTO autofix_runs (repo, pr_number, status) VALUES (?, ?, ?)",
        ("acme/widgets", 3, "running"),
    )
    conn.commit()

    assert count_running_runs(conn) == 2
    assert can_start_new_run(conn, max_running_runs=3) is True
    assert can_start_new_run(conn, max_running_runs=2) is False
