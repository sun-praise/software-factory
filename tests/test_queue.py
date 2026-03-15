from __future__ import annotations

import sqlite3

from app.models import SCHEMA_SQL
from app.services.queue import (
    claim_next_queued_run,
    enqueue_autofix_run,
    mark_run_finished,
    recover_stale_runs,
    touch_run_progress,
)


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    return conn


def test_enqueue_claim_finish_status_flow() -> None:
    conn = _make_conn()
    run_id = enqueue_autofix_run(
        conn=conn,
        repo="acme/widgets",
        pr_number=42,
        head_sha="abc123",
        normalized_review_json={"summary": "1 blocking issue"},
    )
    assert run_id is not None

    first_claim = claim_next_queued_run(conn)
    second_claim = claim_next_queued_run(conn)

    assert first_claim is not None
    assert first_claim["id"] == run_id
    assert first_claim["status"] == "running"
    assert second_claim is None

    mark_run_finished(
        conn=conn,
        run_id=run_id,
        status="success",
        commit_sha="deadbeef",
        logs_path="logs/autofix-run-1.log",
    )

    row = conn.execute("SELECT * FROM autofix_runs WHERE id = ?", (run_id,)).fetchone()
    assert row is not None
    assert row["status"] == "success"
    assert row["commit_sha"] == "deadbeef"
    assert row["logs_path"] == "logs/autofix-run-1.log"
    assert row["finished_at"] is not None


def test_enqueue_autofix_run_deduplicates_idempotency_key() -> None:
    conn = _make_conn()

    first = enqueue_autofix_run(
        conn=conn,
        repo="acme/widgets",
        pr_number=42,
        head_sha="abc123",
        normalized_review_json={"summary": "1 blocking issue"},
        idempotency_key="task:acme/widgets:42:abc123:batch-1",
    )
    second = enqueue_autofix_run(
        conn=conn,
        repo="acme/widgets",
        pr_number=42,
        head_sha="abc123",
        normalized_review_json={"summary": "1 blocking issue"},
        idempotency_key="task:acme/widgets:42:abc123:batch-1",
    )

    assert isinstance(first, int)
    assert second is None


def test_claim_next_queued_run_promotes_due_retry() -> None:
    conn = _make_conn()
    conn.execute(
        """
        INSERT INTO autofix_runs (repo, pr_number, status, retry_after)
        VALUES (?, ?, 'retry_scheduled', '2000-01-01T00:00:00Z')
        """,
        ("acme/widgets", 8),
    )
    conn.commit()

    claimed = claim_next_queued_run(conn, worker_id="worker-a", max_running_runs=2)

    assert claimed is not None
    assert claimed["status"] == "running"
    assert claimed["worker_id"] == "worker-a"


def test_recover_stale_runs_marks_old_running_rows_failed() -> None:
    conn = _make_conn()
    conn.execute(
        """
        INSERT INTO autofix_runs (repo, pr_number, status, worker_id, updated_at)
        VALUES
            (?, ?, 'running', 'worker-a', '2000-01-01 00:00:00'),
            (?, ?, 'cancel_requested', 'worker-a', '2000-01-01 00:00:00'),
            (?, ?, 'running', 'worker-b', CURRENT_TIMESTAMP)
        """,
        ("acme/widgets", 1, "acme/widgets", 2, "acme/widgets", 3),
    )
    conn.commit()

    recovered = recover_stale_runs(
        conn,
        stale_after_seconds=60,
        worker_id="worker-a",
    )

    assert recovered == 2
    rows = conn.execute(
        "SELECT pr_number, status, error_summary, last_error_code FROM autofix_runs ORDER BY pr_number ASC"
    ).fetchall()
    assert rows[0]["status"] == "failed"
    assert rows[0]["error_summary"] == "stale_run_recovered"
    assert rows[0]["last_error_code"] == "stale_run_recovered"
    assert rows[1]["status"] == "failed"
    assert rows[2]["status"] == "running"


def test_touch_run_progress_updates_timestamp_and_logs_path() -> None:
    conn = _make_conn()
    run_id = enqueue_autofix_run(
        conn=conn,
        repo="acme/widgets",
        pr_number=42,
        head_sha="abc123",
        normalized_review_json={"summary": "1 blocking issue"},
    )
    assert run_id is not None

    before = conn.execute(
        "SELECT updated_at, logs_path FROM autofix_runs WHERE id = ?",
        (run_id,),
    ).fetchone()
    assert before is not None

    conn.execute("UPDATE autofix_runs SET updated_at = '2000-01-01 00:00:00' WHERE id = ?", (run_id,))
    conn.commit()

    touch_run_progress(conn, run_id, logs_path="logs/autofix-run-42.log")

    after = conn.execute(
        "SELECT updated_at, logs_path FROM autofix_runs WHERE id = ?",
        (run_id,),
    ).fetchone()
    assert after is not None
    assert after["logs_path"] == "logs/autofix-run-42.log"
    assert after["updated_at"] != "2000-01-01 00:00:00"
