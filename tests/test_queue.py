from __future__ import annotations

import sqlite3

from app.models import SCHEMA_SQL
from app.services.queue import (
    claim_next_queued_run,
    enqueue_autofix_run,
    mark_run_finished,
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
