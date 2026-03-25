from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, cast

from app.services.concurrency import can_start_new_run
from app.services.run_hints import (
    OPERATOR_HINTS_MAX_CHARS,
    OPERATOR_HINT_APPEND_MAX_CHARS,
    OPERATOR_HINT_SEPARATOR,
)


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
    opened_pr_number: int | None = None,
    opened_pr_url: str | None = None,
) -> None:
    conn.execute(
        """
        UPDATE autofix_runs
        SET status = ?,
            commit_sha = ?,
            error_summary = ?,
            logs_path = ?,
            opened_pr_number = COALESCE(?, opened_pr_number),
            opened_pr_url = COALESCE(?, opened_pr_url),
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
            opened_pr_number,
            opened_pr_url,
            last_error_code,
            last_error_code,
            run_id,
        ),
    )
    conn.commit()


def resume_waits_for_baseline_fix(
    conn: sqlite3.Connection,
    repo: str,
    pr_number: int,
    baseline_run_id: int,
    baseline_success: bool,
) -> list[int]:
    """Resume runs waiting for a baseline fix run.

    When a baseline_fix run completes, find all runs in 'waiting_for_baseline_fix'
    status for the same (repo, pr_number) and requeue them.

    Returns list of resumed run IDs.
    """
    resumed_ids: list[int] = []
    cursor = conn.execute(
        """
        SELECT id, operator_hints
        FROM autofix_runs
        WHERE repo = ?
          AND pr_number = ?
          AND status = 'waiting_for_baseline_fix'
        """,
        (repo, pr_number),
    )
    rows = cursor.fetchall()
    for row in rows:
        run_id = int(row["id"])
        hints = str(row["operator_hints"] or "")
        if f"baseline fix run #{baseline_run_id}" in hints:
            new_status = "queued" if baseline_success else "failed"
            note = (
                "Baseline fix succeeded, resuming."
                if baseline_success
                else "Baseline fix failed."
            )
            conn.execute(
                """
                UPDATE autofix_runs
                SET status = ?,
                    operator_hints = operator_hints || ? || CHAR(10),
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (new_status, note, run_id),
            )
            resumed_ids.append(run_id)
    if resumed_ids:
        conn.commit()
    return resumed_ids


def update_run_logs_path(conn: sqlite3.Connection, run_id: int, logs_path: str) -> None:
    conn.execute(
        """
        UPDATE autofix_runs
        SET logs_path = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (logs_path, run_id),
    )
    conn.commit()


def update_run_opened_pr(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    opened_pr_number: int | None,
    opened_pr_url: str | None,
) -> None:
    conn.execute(
        """
        UPDATE autofix_runs
        SET opened_pr_number = ?,
            opened_pr_url = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (opened_pr_number, opened_pr_url, run_id),
    )
    conn.commit()


def get_run_operator_hints(conn: sqlite3.Connection, run_id: int) -> str:
    row = conn.execute(
        "SELECT operator_hints FROM autofix_runs WHERE id = ?",
        (run_id,),
    ).fetchone()
    if row is None:
        return ""
    return str(row["operator_hints"] or "").strip()


def append_run_operator_hint(
    conn: sqlite3.Connection,
    run_id: int,
    text: str,
) -> str | None:
    normalized = str(text).strip()
    if not normalized:
        return None
    if len(normalized) > OPERATOR_HINT_APPEND_MAX_CHARS:
        raise ValueError(
            f"operator hint exceeds max length of {OPERATOR_HINT_APPEND_MAX_CHARS} characters"
        )

    existing = get_run_operator_hints(conn, run_id)
    combined = (
        normalized
        if not existing
        else f"{existing}{OPERATOR_HINT_SEPARATOR}{normalized}"
    )
    if len(combined) > OPERATOR_HINTS_MAX_CHARS:
        raise ValueError(
            f"combined operator hints exceed max length of {OPERATOR_HINTS_MAX_CHARS} characters"
        )
    conn.execute(
        """
        UPDATE autofix_runs
        SET operator_hints = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (combined, run_id),
    )
    conn.commit()
    return combined


def touch_run_progress(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    logs_path: str | None = None,
) -> None:
    if logs_path is None:
        conn.execute(
            """
            UPDATE autofix_runs
            SET updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (run_id,),
        )
    else:
        conn.execute(
            """
            UPDATE autofix_runs
            SET logs_path = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (logs_path, run_id),
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


def recover_stale_runs(
    conn: sqlite3.Connection,
    *,
    stale_after_seconds: int,
    worker_id: str | None = None,
) -> int:
    if stale_after_seconds <= 0:
        return 0

    params: list[Any] = [
        "stale_run_recovered",
        "stale_run_recovered",
        f"-{int(stale_after_seconds)} seconds",
    ]
    worker_filter = ""
    if worker_id:
        worker_filter = "AND worker_id = ?"
        params.append(worker_id)

    cursor = conn.execute(
        f"""
        UPDATE autofix_runs
        SET status = 'failed',
            error_summary = COALESCE(error_summary, ?),
            last_error_code = COALESCE(last_error_code, ?),
            last_error_at = CURRENT_TIMESTAMP,
            finished_at = CURRENT_TIMESTAMP,
            updated_at = CURRENT_TIMESTAMP
        WHERE status IN ('running', 'cancel_requested')
          AND updated_at IS NOT NULL
          AND datetime(updated_at) <= datetime('now', ?)
          {worker_filter}
        """,
        tuple(params),
    )
    conn.commit()
    return int(cursor.rowcount or 0)


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
