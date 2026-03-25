from __future__ import annotations

from datetime import datetime, timezone
import sqlite3

from app.models import SCHEMA_SQL
from app.services.retry import (
    RetryConfig,
    compute_backoff_seconds,
    schedule_retry,
    should_retry,
)


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
        ("acme/widgets", 42, "failed", 0, 3, 1),
    )
    conn.commit()
    run_id = cursor.lastrowid
    assert run_id is not None

    config = RetryConfig(base_delay_seconds=15, max_delay_seconds=120)
    plan = schedule_retry(
        conn,
        run_id,
        error_code="transient_network",
        error_summary="temporary failure",
        now=datetime(2026, 3, 12, 10, 0, tzinfo=timezone.utc),
        config=config,
    )

    row = conn.execute("SELECT * FROM autofix_runs WHERE id = ?", (run_id,)).fetchone()

    assert plan.scheduled is True
    assert plan.delay_seconds == 15
    assert plan.retry_after == "2026-03-12T10:00:15Z"
    assert plan.next_attempt_count == 1
    assert row is not None
    assert row["status"] == "retry_scheduled"
    assert row["attempt_count"] == 1
    assert row["retry_after"] == "2026-03-12T10:00:15Z"
    assert row["last_error_code"] == "transient_network"


def test_schedule_retry_marks_non_retryable_failure() -> None:
    conn = _make_conn()
    cursor = conn.execute(
        """
        INSERT INTO autofix_runs (repo, pr_number, status, attempt_count, max_attempts, retryable)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        ("acme/widgets", 43, "failed", 0, 3, 1),
    )
    conn.commit()
    run_id = cursor.lastrowid
    assert run_id is not None

    config = RetryConfig(non_retryable_error_codes={"fatal"})
    plan = schedule_retry(
        conn,
        run_id,
        error_code="fatal",
        error_summary="not recoverable",
        now=datetime(2026, 3, 12, 10, 0, tzinfo=timezone.utc),
        config=config,
    )

    row = conn.execute("SELECT * FROM autofix_runs WHERE id = ?", (run_id,)).fetchone()

    assert plan.scheduled is False
    assert row is not None
    assert row["status"] == "failed"
    assert row["retryable"] == 0
    assert row["last_error_code"] == "fatal"


def test_schedule_retry_exponential_backoff_grows_with_persisted_attempt_count() -> (
    None
):
    """Scheduling two retries for the same run must use exponential backoff.

    The second scheduled retry should have a larger delay than the first,
    proving that attempt_count is correctly persisted to the database and
    used as the basis for the next retry number calculation.
    """
    conn = _make_conn()
    cursor = conn.execute(
        """
        INSERT INTO autofix_runs (repo, pr_number, status, attempt_count, max_attempts, retryable)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        ("acme/widgets", 45, "failed", 0, 5, 1),
    )
    conn.commit()
    run_id = cursor.lastrowid
    assert run_id is not None

    base_time = datetime(2026, 3, 12, 10, 0, 0, tzinfo=timezone.utc)

    plan1 = schedule_retry(
        conn,
        run_id,
        error_code="transient",
        now=base_time,
        base_delay_seconds=30,
        max_delay_seconds=1800,
    )
    assert plan1.scheduled is True
    assert plan1.delay_seconds == 30
    assert plan1.retry_after == "2026-03-12T10:00:30Z"
    assert plan1.next_attempt_count == 1

    # Attempt count must be persisted so the second call picks up where the
    # first left off, producing a larger delay (exponential backoff).
    plan2 = schedule_retry(
        conn,
        run_id,
        error_code="transient",
        now=datetime(2026, 3, 12, 10, 1, 0, tzinfo=timezone.utc),
        base_delay_seconds=30,
        max_delay_seconds=1800,
    )
    assert plan2.scheduled is True
    assert plan2.retry_after == "2026-03-12T10:02:00Z"
    # With persisted attempt_count=1, next retry number is 2 → 30*2^(2-1)=60s
    assert plan2.delay_seconds == 60
    assert plan2.next_attempt_count == 2

    row = conn.execute(
        "SELECT attempt_count, retry_after FROM autofix_runs WHERE id = ?", (run_id,)
    ).fetchone()
    assert row is not None
    assert row["attempt_count"] == 2
    assert row["retry_after"] == "2026-03-12T10:02:00Z"


def test_schedule_retry_backward_compat_individual_params() -> None:
    conn = _make_conn()
    cursor = conn.execute(
        """
        INSERT INTO autofix_runs (repo, pr_number, status, attempt_count, max_attempts, retryable)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        ("acme/widgets", 44, "failed", 0, 3, 1),
    )
    conn.commit()
    run_id = cursor.lastrowid
    assert run_id is not None

    plan = schedule_retry(
        conn,
        run_id,
        error_code="transient_network",
        error_summary="temporary failure",
        now=datetime(2026, 3, 12, 10, 0, tzinfo=timezone.utc),
        base_delay_seconds=20,
        max_delay_seconds=200,
    )

    row = conn.execute("SELECT * FROM autofix_runs WHERE id = ?", (run_id,)).fetchone()

    assert plan.scheduled is True
    assert plan.delay_seconds == 20
    assert plan.retry_after == "2026-03-12T10:00:20Z"
    assert row is not None
    assert row["status"] == "retry_scheduled"
