import sqlite3

import pytest

from app.models import SCHEMA_SQL
from app.services.policy import (
    ensure_pull_request_row,
    get_autofix_count,
    get_remaining_autofix_quota,
    increment_autofix_count,
    is_autofix_limit_reached,
)


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    return conn


def test_ensure_pull_request_row_initializes_and_updates_metadata() -> None:
    conn = _make_conn()

    row = ensure_pull_request_row(
        conn,
        "acme/widgets",
        42,
        branch="feature/m6",
        head_sha="abc123",
    )

    assert row["repo"] == "acme/widgets"
    assert row["pr_number"] == 42
    assert row["branch"] == "feature/m6"
    assert row["head_sha"] == "abc123"
    assert row["autofix_count"] == 0

    updated = ensure_pull_request_row(
        conn,
        "acme/widgets",
        42,
        head_sha="def456",
    )
    assert updated["head_sha"] == "def456"
    assert updated["branch"] == "feature/m6"


def test_get_remaining_autofix_quota_creates_missing_row() -> None:
    conn = _make_conn()

    remaining = get_remaining_autofix_quota(
        conn,
        "acme/widgets",
        7,
        max_autofix_per_pr=3,
    )

    assert remaining == 3
    assert get_autofix_count(conn, "acme/widgets", 7) == 0


def test_increment_autofix_count_updates_count_and_remaining_quota() -> None:
    conn = _make_conn()

    current = increment_autofix_count(
        conn,
        "acme/widgets",
        9,
        amount=2,
        branch="feature/m6",
        head_sha="abc123",
    )

    assert current == 2
    assert (
        get_remaining_autofix_quota(
            conn,
            "acme/widgets",
            9,
            max_autofix_per_pr=3,
        )
        == 1
    )


def test_is_autofix_limit_reached_when_count_hits_limit() -> None:
    conn = _make_conn()
    increment_autofix_count(conn, "acme/widgets", 11, amount=3)

    assert (
        is_autofix_limit_reached(
            conn,
            "acme/widgets",
            11,
            max_autofix_per_pr=3,
        )
        is True
    )


def test_increment_autofix_count_rejects_non_positive_amount() -> None:
    conn = _make_conn()

    with pytest.raises(ValueError, match="amount must be positive"):
        increment_autofix_count(conn, "acme/widgets", 13, amount=0)


def test_get_remaining_autofix_quota_rejects_negative_limit() -> None:
    conn = _make_conn()

    with pytest.raises(ValueError, match="max_autofix_per_pr must be non-negative"):
        get_remaining_autofix_quota(
            conn,
            "acme/widgets",
            15,
            max_autofix_per_pr=-1,
        )
