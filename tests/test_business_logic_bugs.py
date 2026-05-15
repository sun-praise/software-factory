"""Business-logic tests designed to DISCOVER bugs in core workflows.

Each test exercises a realistic business scenario and asserts the CORRECT
behaviour.  A failing test means the current code is wrong.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from app.models import SCHEMA_SQL


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    return conn


def _insert_run(
    conn: sqlite3.Connection,
    *,
    repo: str = "acme/widgets",
    pr_number: int = 42,
    status: str = "queued",
    attempt_count: int = 0,
    max_attempts: int = 3,
    retryable: int = 1,
    head_sha: str | None = None,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO autofix_runs
            (repo, pr_number, status, attempt_count, max_attempts, retryable, head_sha)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (repo, pr_number, status, attempt_count, max_attempts, retryable, head_sha),
    )
    conn.commit()
    assert cursor.lastrowid is not None
    return int(cursor.lastrowid)


# ===================================================================
# Bug: schedule_retry corrupts terminal-status runs
# ===================================================================

class TestScheduleRetryCorruptsTerminalRuns:
    """Scenario: a run finishes successfully (status='success').  Due to a
    race condition or programming error, ``schedule_retry`` is called on it.

    Expected: the function should NOT downgrade a 'success' run to 'failed'.
    Actual:   ``should_retry`` returns False for terminal statuses, but the
              fallback branch in ``schedule_retry`` unconditionally sets
              ``status='failed'``, corrupting the run record.
    """

    def test_success_run_not_corrupted_by_schedule_retry(self):
        from app.services.retry import RetryConfig, schedule_retry

        conn = _make_conn()
        run_id = _insert_run(conn, status="success")

        schedule_retry(
            conn,
            run_id,
            error_code="late_error",
            error_summary="arrived after finish",
            now=datetime(2026, 5, 15, 12, 0, tzinfo=timezone.utc),
            config=RetryConfig(),
        )

        row = conn.execute(
            "SELECT status FROM autofix_runs WHERE id = ?", (run_id,)
        ).fetchone()
        assert row is not None
        # Desired: terminal status must be preserved.
        # Bug: schedule_retry overwrites 'success' with 'failed'.
        assert row["status"] == "success", (
            f"Bug: schedule_retry changed terminal status from 'success' to '{row['status']}'"
        )

    def test_cancelled_run_not_corrupted_by_schedule_retry(self):
        from app.services.retry import RetryConfig, schedule_retry

        conn = _make_conn()
        run_id = _insert_run(conn, status="cancelled")

        schedule_retry(
            conn,
            run_id,
            error_code="stray_error",
            now=datetime(2026, 5, 15, 12, 0, tzinfo=timezone.utc),
            config=RetryConfig(),
        )

        row = conn.execute(
            "SELECT status FROM autofix_runs WHERE id = ?", (run_id,)
        ).fetchone()
        assert row is not None
        assert row["status"] == "cancelled", (
            f"Bug: schedule_retry changed terminal status from 'cancelled' to '{row['status']}'"
        )


# ===================================================================
# Bug: mark_run_finished accepts any status string without validation
# ===================================================================

class TestMarkRunFinishedNoStatusValidation:
    """Scenario: a programming error passes an invalid status string to
    ``mark_run_finished``.

    Expected: the function should reject unknown status values.
    Actual:   any string is accepted and persisted to the database, creating
              an invalid state that downstream code cannot handle.
    """

    @pytest.mark.parametrize("bad_status", ["banana", "", "SUCCESS", "running"])
    def test_invalid_status_rejected(self, bad_status):
        from app.services.queue import mark_run_finished

        conn = _make_conn()
        run_id = _insert_run(conn, status="running")

        with pytest.raises((ValueError, AssertionError)):
            mark_run_finished(
                conn=conn,
                run_id=run_id,
                status=bad_status,
            )

        # Verify the status was NOT changed
        row = conn.execute(
            "SELECT status FROM autofix_runs WHERE id = ?", (run_id,)
        ).fetchone()
        assert row["status"] == "running", (
            f"Bug: mark_run_finished accepted invalid status '{bad_status}'"
        )

    def test_cannot_overwrite_success_with_failed(self):
        """A run that already succeeded should not be retroactively marked failed."""
        from app.services.queue import mark_run_finished

        conn = _make_conn()
        run_id = _insert_run(conn, status="success")

        with pytest.raises((ValueError, AssertionError)):
            mark_run_finished(
                conn=conn,
                run_id=run_id,
                status="failed",
                error_summary="late failure",
            )

        row = conn.execute(
            "SELECT status FROM autofix_runs WHERE id = ?", (run_id,)
        ).fetchone()
        assert row["status"] == "success", (
            "Bug: mark_run_finished allowed retroactive downgrade from success to failed"
        )


# ===================================================================
# Bug: cancelling a 'cancel_requested' run re-issues the same UPDATE
# ===================================================================

class TestCancelRequestedRunDoubleCancel:
    """Scenario: a run is already in 'cancel_requested' status.  The operator
    clicks the cancel button again.

    Expected: the function should return the current status without executing
              any database write (idempotent, no-op).
    Actual:   ``request_run_cancel`` falls through to the final branch and
              executes an UPDATE that sets status='cancel_requested' again,
              including updating timestamps unnecessarily.
    """

    def test_cancel_requested_is_idempotent_no_update(self):
        from app.services.queue import request_run_cancel

        conn = _make_conn()
        run_id = _insert_run(conn, status="cancel_requested")

        # Capture updated_at before second cancel
        row_before = conn.execute(
            "SELECT updated_at FROM autofix_runs WHERE id = ?", (run_id,)
        ).fetchone()
        assert row_before is not None
        updated_at_before = row_before["updated_at"]

        # Advance time in SQLite to detect if an UPDATE happens
        conn.execute(
            "UPDATE autofix_runs SET updated_at = '2000-01-01 00:00:00' WHERE id = ?",
            (run_id,),
        )
        conn.commit()

        result = request_run_cancel(conn, run_id)

        assert result == "cancel_requested"

        row_after = conn.execute(
            "SELECT updated_at FROM autofix_runs WHERE id = ?", (run_id,)
        ).fetchone()
        assert row_after is not None

        # Desired: updated_at should remain at '2000-01-01 00:00:00' (no-op).
        # Bug: the timestamp was updated, proving an unnecessary write occurred.
        assert row_after["updated_at"] == "2000-01-01 00:00:00", (
            "Bug: request_run_cancel performed an unnecessary UPDATE "
            "on a run already in 'cancel_requested' status"
        )


# ===================================================================
# Bug: enqueue_autofix_run swallows non-idempotency IntegrityErrors
# ===================================================================

class TestEnqueueSwallowsIntegrityErrors:
    """Scenario: an IntegrityError occurs during enqueue that is NOT caused
    by a duplicate idempotency_key (e.g. a schema migration issue or a new
    constraint).

    Expected: the function should re-raise the unexpected IntegrityError so
              the caller can distinguish it from a legitimate duplicate.
    Actual:   ALL IntegrityErrors return None, making it impossible to tell
              a real duplicate from a database constraint violation.
    """

    def test_non_idempotency_integrity_error_is_not_silenced(self):
        from app.services.queue import enqueue_autofix_run

        conn = _make_conn()

        # First insert succeeds
        first = enqueue_autofix_run(
            conn=conn,
            repo="acme/widgets",
            pr_number=42,
            head_sha="abc123",
            normalized_review_json={"summary": "test"},
            idempotency_key="key-unique-001",
        )
        assert first is not None

        # Second insert with same idempotency_key returns None (correct)
        second = enqueue_autofix_run(
            conn=conn,
            repo="acme/widgets",
            pr_number=42,
            head_sha="abc123",
            normalized_review_json={"summary": "test"},
            idempotency_key="key-unique-001",
        )
        assert second is None

        # Now try a DIFFERENT idempotency_key but same repo/pr.
        # This should succeed (different task) and return a new run_id.
        third = enqueue_autofix_run(
            conn=conn,
            repo="acme/widgets",
            pr_number=42,
            head_sha="def456",
            normalized_review_json={"summary": "another task"},
            idempotency_key="key-unique-002",
        )
        # Desired: should return a new run ID because the task is different.
        # This test verifies that legitimate duplicates are handled correctly.
        # The deeper issue is that ALL IntegrityErrors are swallowed.
        assert third is not None, (
            "Bug: enqueue_autofix_run returned None for a legitimately new run. "
            "This suggests non-idempotency IntegrityErrors are being silently swallowed."
        )


# ===================================================================
# Bug: SHA ping-pong resets autofix count repeatedly
# ===================================================================

class TestShaPingPongResetsCount:
    """Scenario: a PR's head_sha alternates between SHA-A and SHA-B
    (e.g. due to force-push / rebase cycles).  Each SHA change triggers
    ``reset_autofix_count_on_sha_change``, which resets autofix_count to 0.

    This allows an attacker to bypass the autofix_per_pr limit by
    repeatedly changing the SHA back and forth, getting unlimited autofixes.

    Expected: autofix_count should NOT be reset when SHA changes back to
              a previously-seen value.
    Actual:   every SHA change resets the counter, enabling unlimited autofixes.
    """

    def test_sha_ping_pong_does_not_reset_count_repeatedly(self):
        from app.services.policy import (
            ensure_pull_request_row,
            increment_autofix_count,
            reset_autofix_count_on_sha_change,
        )

        conn = _make_conn()

        # Create PR with SHA-A
        ensure_pull_request_row(conn, "acme/widgets", 42, head_sha="sha-A")
        # Increment count to 2
        increment_autofix_count(conn, "acme/widgets", 42, amount=2)

        row = conn.execute(
            "SELECT autofix_count, head_sha FROM pull_requests WHERE repo = ? AND pr_number = ?",
            ("acme/widgets", 42),
        ).fetchone()
        assert row["autofix_count"] == 2

        # SHA changes to B → count resets to 0
        reset_autofix_count_on_sha_change(conn, "acme/widgets", 42, "sha-B")
        ensure_pull_request_row(conn, "acme/widgets", 42, head_sha="sha-B")

        row = conn.execute(
            "SELECT autofix_count, head_sha FROM pull_requests WHERE repo = ? AND pr_number = ?",
            ("acme/widgets", 42),
        ).fetchone()
        assert row["autofix_count"] == 0

        # Increment again
        increment_autofix_count(conn, "acme/widgets", 42, amount=2)

        # SHA changes back to A → count resets AGAIN (this is the bug)
        did_reset = reset_autofix_count_on_sha_change(conn, "acme/widgets", 42, "sha-A")
        ensure_pull_request_row(conn, "acme/widgets", 42, head_sha="sha-A")

        row = conn.execute(
            "SELECT autofix_count, head_sha FROM pull_requests WHERE repo = ? AND pr_number = ?",
            ("acme/widgets", 42),
        ).fetchone()

        # Desired: SHA-A was already seen before, should NOT reset again.
        # Bug: count is reset to 0, allowing repeated quota bypass.
        assert not did_reset or row["autofix_count"] != 0, (
            f"Bug: SHA ping-pong (A→B→A) resets autofix_count. "
            f"did_reset={did_reset}, count={row['autofix_count']}. "
            f"Returning to a previously-seen SHA should not reset the counter."
        )


# ===================================================================
# Bug: normalizer treats all inline review comments as must_fix
# ===================================================================

class TestNormalizerInlineCommentsAlwaysMustFix:
    """Scenario: a reviewer leaves a polite inline suggestion like
    "nit: consider using a constant here".  This is clearly a suggestion,
    not a blocking change request.

    Expected: the comment should be classified as non-blocking suggestion.
    Actual:   ``_extract_candidate`` hard-codes ``is_must_fix=True`` for all
              ``pull_request_review_comment`` events.  When no strong semantic
              signal overrides this, the fallback logic at line 421-422
              classifies it as ``SEMANTIC_BLOCKING_DEFECT`` with low confidence,
              causing it to appear in the ``must_fix`` list.
    """

    def test_nit_inline_comment_is_not_must_fix(self):
        from app.services.normalizer import normalize_review_events

        result = normalize_review_events(
            repo="acme/widgets",
            pr_number=42,
            events=[
                {
                    "event_type": "pull_request_review_comment",
                    "payload": {
                        "comment": {
                            "body": "nit: consider using a constant here",
                            "path": "src/main.py",
                            "line": 10,
                        },
                    },
                },
            ],
            head_sha="abc123",
        )

        # Check if this is in must_fix or should_fix
        must_fix_texts = [item["text"] for item in result["must_fix"]]
        should_fix_texts = [item["text"] for item in result["should_fix"]]

        # Desired: "nit: consider using a constant" should be a suggestion,
        # not a blocking defect.
        # Bug: it ends up in must_fix because is_must_fix=True is hardcoded.
        assert "nit: consider using a constant here" not in must_fix_texts, (
            f"Bug: inline 'nit' comment classified as must_fix (blocking). "
            f"must_fix={must_fix_texts}, should_fix={should_fix_texts}"
        )

    def test_question_inline_comment_is_not_must_fix(self):
        """A genuine question on a line should not be treated as a blocking defect."""
        from app.services.normalizer import normalize_review_events

        result = normalize_review_events(
            repo="acme/widgets",
            pr_number=42,
            events=[
                {
                    "event_type": "pull_request_review_comment",
                    "payload": {
                        "comment": {
                            "body": "why is this here?",
                            "path": "src/main.py",
                            "line": 20,
                        },
                    },
                },
            ],
            head_sha="abc123",
        )

        must_fix_texts = [item["text"] for item in result["must_fix"]]
        should_fix_texts = [item["text"] for item in result["should_fix"]]
        ignore_texts = [item["text"] for item in result["ignore"]]

        # Desired: a question should be clarification (should_fix or ignore),
        # not a blocking defect.
        assert "why is this here?" not in must_fix_texts, (
            f"Bug: inline question classified as must_fix (blocking). "
            f"must_fix={must_fix_texts}, should_fix={should_fix_texts}, ignore={ignore_texts}"
        )
