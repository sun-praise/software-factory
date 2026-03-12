from __future__ import annotations

from datetime import datetime, timezone
import sqlite3

from app.models import SCHEMA_SQL
from app.services.retry import compute_backoff_seconds, schedule_retry, should_retry


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    return conn


def test_should_retry_respects_limits_and_non_retryable_codes() -> None:
    assert should_retry(status="failed", attempt_count=1, max_attempts=3) is True
    assert should_retry(status="success", attempt_count=1, max_attempts=3) is False
    assert should_retry(status="failed", attempt_count=3, max_attempts=3) is False
    assert (
        should_retry(
            status="failed",
            attempt_count=1,
            max_attempts=3,
            error_code="fatal",
            non_retryable_error_codes={"fatal"},
        )
        is False
    )


def test_compute_backoff_seconds_uses_exponential_cap() -> None:
    assert (
        compute_backoff_seconds(retry_number=1, base_seconds=10, max_seconds=60) == 10
    )
    assert (
        compute_backoff_seconds(retry_number=2, base_seconds=10, max_seconds=60) == 20
    )
    assert (
        compute_backoff_seconds(retry_number=4, base_seconds=10, max_seconds=60) == 60
    )


def test_schedule_retry_updates_retry_fields() -> None:
    conn = _make_conn()
    cursor = conn.execute(
        """
        INSERT INTO autofix_runs (repo, pr_number, status, attempt_count, max_attempts, retryable)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        ("acme/widgets", 42, "failed", 1, 3, 1),
    )
    conn.commit()
    run_id = int(cursor.lastrowid)

    plan = schedule_retry(
        conn,
        run_id,
        error_code="transient_network",
        error_summary="temporary failure",
        now=datetime(2026, 3, 12, 10, 0, tzinfo=timezone.utc),
        base_delay_seconds=15,
        max_delay_seconds=120,
    )

    row = conn.execute("SELECT * FROM autofix_runs WHERE id = ?", (run_id,)).fetchone()

    assert plan.scheduled is True
    assert plan.delay_seconds == 15
    assert plan.retry_after == "2026-03-12T10:00:15Z"
    assert plan.next_attempt_count == 2
    assert row is not None
    assert row["status"] == "retry_scheduled"
    assert row["attempt_count"] == 2
    assert row["retry_after"] == "2026-03-12T10:00:15Z"
    assert row["last_error_code"] == "transient_network"


def test_schedule_retry_marks_non_retryable_failure() -> None:
    conn = _make_conn()
    cursor = conn.execute(
        """
        INSERT INTO autofix_runs (repo, pr_number, status, attempt_count, max_attempts, retryable)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        ("acme/widgets", 43, "failed", 1, 3, 1),
    )
    conn.commit()
    run_id = int(cursor.lastrowid)

    plan = schedule_retry(
        conn,
        run_id,
        error_code="fatal",
        error_summary="not recoverable",
        now=datetime(2026, 3, 12, 10, 0, tzinfo=timezone.utc),
        non_retryable_error_codes={"fatal"},
    )

    row = conn.execute("SELECT * FROM autofix_runs WHERE id = ?", (run_id,)).fetchone()

    assert plan.scheduled is False
    assert row is not None
    assert row["status"] == "failed"
    assert row["retryable"] == 0
    assert row["last_error_code"] == "fatal"
