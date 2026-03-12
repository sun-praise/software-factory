from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any


@dataclass(frozen=True)
class PRLock:
    repo: str
    pr_number: int
    lock_owner: str | None
    lock_run_id: int | None
    lock_acquired_at: str | None
    lock_expires_at: str | None


def acquire_pr_lock(
    conn: sqlite3.Connection,
    repo: str,
    pr_number: int,
    lock_owner: str,
    lock_ttl_seconds: int = 900,
    run_id: int | None = None,
    now: datetime | None = None,
) -> bool:
    if lock_ttl_seconds <= 0:
        raise ValueError("lock_ttl_seconds must be positive")

    current_time = _normalize_now(now)
    acquired_at = _to_timestamp(current_time)
    expires_at = _to_timestamp(current_time + timedelta(seconds=lock_ttl_seconds))

    conn.execute(
        "INSERT OR IGNORE INTO pull_requests (repo, pr_number) VALUES (?, ?)",
        (repo, pr_number),
    )
    cursor = conn.execute(
        """
        UPDATE pull_requests
        SET lock_owner = ?,
            lock_run_id = ?,
            lock_acquired_at = ?,
            lock_expires_at = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE repo = ?
          AND pr_number = ?
          AND (
              lock_owner IS NULL
              OR lock_expires_at IS NULL
              OR lock_expires_at <= ?
              OR lock_owner = ?
          )
        """,
        (
            lock_owner,
            run_id,
            acquired_at,
            expires_at,
            repo,
            pr_number,
            acquired_at,
            lock_owner,
        ),
    )
    conn.commit()
    return cursor.rowcount > 0


def release_pr_lock(
    conn: sqlite3.Connection,
    repo: str,
    pr_number: int,
    lock_owner: str | None = None,
    run_id: int | None = None,
    force: bool = False,
) -> bool:
    conditions = ["repo = ?", "pr_number = ?"]
    parameters: list[Any] = [repo, pr_number]

    if not force:
        if lock_owner is not None:
            conditions.append("lock_owner = ?")
            parameters.append(lock_owner)
        if run_id is not None:
            conditions.append("lock_run_id = ?")
            parameters.append(run_id)

    cursor = conn.execute(
        f"""
        UPDATE pull_requests
        SET lock_owner = NULL,
            lock_run_id = NULL,
            lock_acquired_at = NULL,
            lock_expires_at = NULL,
            updated_at = CURRENT_TIMESTAMP
        WHERE {" AND ".join(conditions)}
        """,
        tuple(parameters),
    )
    conn.commit()
    return cursor.rowcount > 0


def get_pr_lock(conn: sqlite3.Connection, repo: str, pr_number: int) -> PRLock | None:
    row = conn.execute(
        """
        SELECT repo, pr_number, lock_owner, lock_run_id, lock_acquired_at, lock_expires_at
        FROM pull_requests
        WHERE repo = ? AND pr_number = ?
        LIMIT 1
        """,
        (repo, pr_number),
    ).fetchone()
    if row is None:
        return None

    return PRLock(
        repo=str(row["repo"]),
        pr_number=int(row["pr_number"]),
        lock_owner=_as_text(row["lock_owner"]),
        lock_run_id=_as_int(row["lock_run_id"]),
        lock_acquired_at=_as_text(row["lock_acquired_at"]),
        lock_expires_at=_as_text(row["lock_expires_at"]),
    )


def count_running_runs(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS count FROM autofix_runs WHERE status = 'running'"
    ).fetchone()
    if row is None:
        return 0
    return int(row["count"])


def can_start_new_run(conn: sqlite3.Connection, max_running_runs: int) -> bool:
    if max_running_runs <= 0:
        raise ValueError("max_running_runs must be positive")
    return count_running_runs(conn) < max_running_runs


def _normalize_now(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


def _to_timestamp(value: datetime) -> str:
    return value.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _as_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)
