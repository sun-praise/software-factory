from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from typing import Any


def enqueue_autofix_run(
    conn: sqlite3.Connection,
    repo: str,
    pr_number: int,
    head_sha: str | None,
    normalized_review_json: Mapping[str, Any],
    trigger_source: str = "github_webhook",
) -> int:
    payload_json = json.dumps(normalized_review_json, ensure_ascii=True, sort_keys=True)
    cursor = conn.execute(
        """
        INSERT INTO autofix_runs (repo, pr_number, head_sha, status, trigger_source, normalized_review_json)
        VALUES (?, ?, ?, 'queued', ?, ?)
        """,
        (repo, pr_number, head_sha, trigger_source, payload_json),
    )
    return int(cursor.lastrowid)


def claim_next_queued_run(conn: sqlite3.Connection) -> dict[str, Any] | None:
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
        SET status = 'running'
        WHERE id = (SELECT id FROM picked)
        RETURNING *
        """
    )
    row = cursor.fetchone()
    if row is None:
        return None
    return _to_dict(row, cursor)


def mark_run_finished(
    conn: sqlite3.Connection,
    run_id: int,
    status: str,
    commit_sha: str | None = None,
    error_summary: str | None = None,
    logs_path: str | None = None,
) -> None:
    conn.execute(
        """
        UPDATE autofix_runs
        SET status = ?,
            commit_sha = ?,
            error_summary = ?,
            logs_path = ?,
            finished_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (status, commit_sha, error_summary, logs_path, run_id),
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
