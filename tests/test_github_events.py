import sqlite3

from app.models import SCHEMA_SQL
from app.services.github_events import extract_review_event, insert_review_event


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_SQL)
    return conn


def test_extract_pull_request_review_event() -> None:
    payload = {
        "repository": {"full_name": "acme/widgets"},
        "pull_request": {"number": 42, "head": {"sha": "abc123"}},
        "review": {"id": 1001, "user": {"login": "reviewer-1"}},
        "sender": {"login": "reviewer-1"},
    }

    event = extract_review_event("pull_request_review", payload)

    assert event is not None
    assert event.repo == "acme/widgets"
    assert event.pr_number == 42
    assert event.head_sha == "abc123"
    assert event.actor == "reviewer-1"
    assert event.event_id == "1001"
    assert event.event_key == "gh:pull_request_review:acme/widgets:42:1001"


def test_extract_pull_request_review_comment_event() -> None:
    payload = {
        "repository": {"full_name": "acme/widgets"},
        "pull_request": {"number": 7, "head": {"sha": "def456"}},
        "comment": {
            "id": 2002,
            "commit_id": "def456",
            "user": {"login": "reviewer-2"},
        },
        "sender": {"login": "reviewer-2"},
    }

    event = extract_review_event("pull_request_review_comment", payload)

    assert event is not None
    assert event.repo == "acme/widgets"
    assert event.pr_number == 7
    assert event.head_sha == "def456"
    assert event.actor == "reviewer-2"
    assert event.event_id == "2002"
    assert event.event_key == "gh:pull_request_review_comment:acme/widgets:7:2002"


def test_extract_issue_comment_for_pr_event() -> None:
    payload = {
        "repository": {"full_name": "acme/widgets"},
        "issue": {"number": 9, "pull_request": {"url": "https://example.test/pr/9"}},
        "comment": {"id": 3003, "user": {"login": "reviewer-3"}},
        "sender": {"login": "reviewer-3"},
    }

    event = extract_review_event("issue_comment", payload)

    assert event is not None
    assert event.repo == "acme/widgets"
    assert event.pr_number == 9
    assert event.head_sha is None
    assert event.actor == "reviewer-3"
    assert event.event_id == "3003"
    assert event.event_key == "gh:issue_comment:acme/widgets:9:3003"


def test_ignore_issue_comment_without_pr_reference() -> None:
    payload = {
        "repository": {"full_name": "acme/widgets"},
        "issue": {"number": 9},
        "comment": {"id": 3004, "user": {"login": "reviewer-4"}},
        "sender": {"login": "reviewer-4"},
    }

    event = extract_review_event("issue_comment", payload)

    assert event is None


def test_insert_review_event_is_idempotent_by_event_key() -> None:
    conn = _make_conn()
    payload = {
        "repository": {"full_name": "acme/widgets"},
        "pull_request": {"number": 42, "head": {"sha": "abc123"}},
        "review": {"id": 1001, "user": {"login": "reviewer-1"}},
        "sender": {"login": "reviewer-1"},
    }
    event = extract_review_event("pull_request_review", payload)
    assert event is not None

    first = insert_review_event(conn, event)
    second = insert_review_event(conn, event)

    row = conn.execute("SELECT COUNT(*) AS cnt FROM review_events").fetchone()
    assert row is not None
    assert first == "inserted"
    assert second == "duplicate"
    assert row[0] == 1
