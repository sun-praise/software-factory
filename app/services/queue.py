from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, cast

from app.services.concurrency import can_start_new_run


def enqueue_autofix_run(
    conn: sqlite3.Connection,
    repo: str,
    pr_number: int,
    head_sha: str | None,
    normalized_review_json: Mapping[str, Any],
    trigger_source: str = "github_webhook",
    *,
    idempotency_key: str | None = None,
    max_attempts: int = 3,
    retryable: bool = True,
) -> int | None:
    payload_json = json.dumps(normalized_review_json, ensure_ascii=True, sort_keys=True)
    try:
        cursor = conn.execute(
            """
            INSERT INTO autofix_runs (
                repo,
                pr_number,
                head_sha,
                status,
                trigger_source,
                idempotency_key,
                normalized_review_json,
                max_attempts,
                retryable,
                updated_at
            )
            VALUES (?, ?, ?, 'queued', ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (
                repo,
                pr_number,
                head_sha,
                trigger_source,
                idempotency_key,
                payload_json,
                max_attempts,
                int(retryable),
            ),
        )
    except sqlite3.IntegrityError:
        return None

    conn.commit()
    lastrowid = cursor.lastrowid
    if lastrowid is None:
        raise RuntimeError("Failed to get inserted autofix_run id")
    return cast(int, lastrowid)


def claim_next_queued_run(
    conn: sqlite3.Connection,
    *,
    worker_id: str | None = None,
    max_running_runs: int | None = None,
) -> dict[str, Any] | None:
    conn.execute("BEGIN IMMEDIATE")
    _promote_due_retries(conn)
    if max_running_runs is not None and not can_start_new_run(conn, max_running_runs):
        conn.rollback()
        return None

    timestamp = _utc_now_timestamp()
    try:
        cursor = conn.execute(
            """
            WITH picked AS (
                SELECT id
                FROM autofix_runs
                WHERE status = 'queued'
                ORDER BY id ASC
                LIMIT 1
            )
            UPDATE autofix_runs
            SET status = 'running',
                worker_id = COALESCE(?, worker_id),
                claimed_at = COALESCE(claimed_at, ?),
                started_at = COALESCE(started_at, ?),
                updated_at = CURRENT_TIMESTAMP
            WHERE id = (SELECT id FROM picked)
            RETURNING *
            """,
            (worker_id, timestamp, timestamp),
        )
        row = cursor.fetchone()
        if row is None:
            conn.rollback()
            return None
        conn.commit()
        return _to_dict(row, cursor)
    except sqlite3.OperationalError as exc:
        if "RETURNING" not in str(exc).upper():
            raise

    selected = conn.execute(
        """
        SELECT id
        FROM autofix_runs
        WHERE status = 'queued'
        ORDER BY id ASC
        LIMIT 1
        """
    ).fetchone()
    if selected is None:
        conn.rollback()
        return None

    run_id = int(selected["id"] if isinstance(selected, sqlite3.Row) else selected[0])
    conn.execute(
        """
        UPDATE autofix_runs
        SET status = 'running',
            worker_id = COALESCE(?, worker_id),
            claimed_at = COALESCE(claimed_at, ?),
            started_at = COALESCE(started_at, ?),
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (worker_id, timestamp, timestamp, run_id),
    )
    row = conn.execute("SELECT * FROM autofix_runs WHERE id = ?", (run_id,)).fetchone()
    conn.commit()
    if row is None:
        return None
    if isinstance(row, sqlite3.Row):
        return {key: row[key] for key in row.keys()}
    if isinstance(row, tuple):
        cursor = conn.execute("SELECT * FROM autofix_runs WHERE id = ?", (run_id,))
        keys = [item[0] for item in (cursor.description or [])]
        return {key: value for key, value in zip(keys, row, strict=False)}
    return _to_dict(row, conn.cursor())


def mark_run_finished(
    conn: sqlite3.Connection,
    run_id: int,
    status: str,
    commit_sha: str | None = None,
    error_summary: str | None = None,
    logs_path: str | None = None,
    last_error_code: str | None = None,
) -> None:
    conn.execute(
        """
        UPDATE autofix_runs
        SET status = ?,
            commit_sha = ?,
            error_summary = ?,
            logs_path = ?,
            last_error_code = COALESCE(?, last_error_code),
            last_error_at = CASE WHEN ? IS NULL THEN last_error_at ELSE CURRENT_TIMESTAMP END,
            finished_at = CURRENT_TIMESTAMP,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (
            status,
            commit_sha,
            error_summary,
            logs_path,
            last_error_code,
            last_error_code,
            run_id,
        ),
    )
    conn.commit()


def get_run_status(conn: sqlite3.Connection, run_id: int) -> str | None:
    row = conn.execute(
        "SELECT status FROM autofix_runs WHERE id = ?",
        (run_id,),
    ).fetchone()
    if row is None:
        return None
    return str(row["status"])


def is_run_cancel_requested(conn: sqlite3.Connection, run_id: int) -> bool:
    return get_run_status(conn, run_id) == "cancel_requested"


def request_run_cancel(conn: sqlite3.Connection, run_id: int) -> str | None:
    current_status = get_run_status(conn, run_id)
    if current_status is None:
        return None

    if current_status in {"success", "failed", "cancelled"}:
        return current_status

    if current_status in {"queued", "retry_scheduled"}:
        conn.execute(
            """
            UPDATE autofix_runs
            SET status = 'cancelled',
                error_summary = 'cancelled_by_user',
                last_error_code = 'cancelled',
                last_error_at = CURRENT_TIMESTAMP,
                finished_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (run_id,),
        )
        conn.commit()
        return "cancelled"

    conn.execute(
        """
        UPDATE autofix_runs
        SET status = 'cancel_requested',
            error_summary = 'cancel_requested_by_user',
            last_error_code = 'cancel_requested',
            last_error_at = CURRENT_TIMESTAMP,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (run_id,),
    )
    conn.commit()
    return "cancel_requested"


def _promote_due_retries(conn: sqlite3.Connection) -> None:
    """
    将到期的重试任务从 'retry_scheduled' 状态提升为 'queued' 状态。

    注意：此函数不执行 commit，由调用者统一管理事务，避免多次 commit 导致的性能问题。
    """
    now_value = _utc_now_timestamp()
    conn.execute(
        """
        UPDATE autofix_runs
        SET status = 'queued',
            updated_at = CURRENT_TIMESTAMP
        WHERE status = 'retry_scheduled'
          AND retry_after IS NOT NULL
          AND retry_after <= ?
        """,
        (now_value,),
    )


def _utc_now_timestamp() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _to_dict(row: Any, cursor: sqlite3.Cursor) -> dict[str, Any]:
    if isinstance(row, sqlite3.Row):
        return {key: row[key] for key in row.keys()}

    if isinstance(row, Mapping):
        return dict(row)

    if isinstance(row, tuple):
        keys = [item[0] for item in (cursor.description or [])]
        return {key: value for key, value in zip(keys, row, strict=False)}

    raise TypeError(f"Unsupported row type: {type(row)!r}")
