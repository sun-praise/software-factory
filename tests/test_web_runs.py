from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import get_settings
from app.db import init_db
from app.main import app
from app.routes import web as web_routes


def _setup_db(tmp_path: Path) -> Path:
    get_settings.cache_clear()
    db_path = tmp_path / "software_factory.db"
    os.environ["DB_PATH"] = str(db_path)
    init_db()
    return db_path


def test_escape_like_pattern_plain() -> None:
    assert web_routes._escape_like_pattern("hello") == "hello"


def test_escape_like_pattern_percent() -> None:
    assert web_routes._escape_like_pattern("100%") == "100\\%"


def test_escape_like_pattern_underscore() -> None:
    assert web_routes._escape_like_pattern("test_1") == "test\\_1"


def test_escape_like_pattern_backslash() -> None:
    assert web_routes._escape_like_pattern("a\\b") == "a\\\\b"


def test_escape_like_pattern_combined() -> None:
    assert web_routes._escape_like_pattern("a%b_c\\d") == "a\\%b\\_c\\\\d"


def test_fetch_runs_search_with_like_wildcards(tmp_path: Path) -> None:
    db_path = _setup_db(tmp_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO autofix_runs (repo, pr_number, trigger_source, status, normalized_review_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("acme/test_1", 1, "manual_issue", "success", "{}"),
        )
        conn.execute(
            """
            INSERT INTO autofix_runs (repo, pr_number, trigger_source, status, normalized_review_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("acme/testX1", 2, "manual_issue", "success", "{}"),
        )
        conn.execute(
            """
            INSERT INTO autofix_runs (repo, pr_number, trigger_source, status, normalized_review_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("acme/100%done", 3, "manual_issue", "success", "{}"),
        )
        conn.execute(
            """
            INSERT INTO autofix_runs (repo, pr_number, trigger_source, status, normalized_review_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("acme/100Xdone", 4, "manual_issue", "success", "{}"),
        )
        conn.execute(
            """
            INSERT INTO autofix_runs (repo, pr_number, trigger_source, status, normalized_review_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("acme/a\\b", 5, "manual_issue", "success", "{}"),
        )
        conn.execute(
            """
            INSERT INTO autofix_runs (repo, pr_number, trigger_source, status, normalized_review_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("acme/aXb", 6, "manual_issue", "success", "{}"),
        )
        conn.commit()

    with TestClient(app) as client:
        underscore = client.get("/", params={"q": "test_1"})
        percent = client.get("/", params={"q": "100%"})
        backslash = client.get("/", params={"q": "a\\b"})
        plain = client.get("/", params={"q": "test"})

    assert underscore.status_code == 200
    assert "acme/test_1" in underscore.text
    assert "acme/testX1" not in underscore.text

    assert percent.status_code == 200
    assert "acme/100%done" in percent.text
    assert "acme/100Xdone" not in percent.text

    assert backslash.status_code == 200
    assert "acme/a\\b" in backslash.text
    assert "acme/aXb" not in backslash.text

    assert plain.status_code == 200
    assert "acme/test_1" in plain.text
    assert "acme/testX1" in plain.text


def test_manual_issue_run_detail_omits_fake_pull_request_link(tmp_path: Path) -> None:
    db_path = _setup_db(tmp_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO autofix_runs (repo, pr_number, trigger_source, status, normalized_review_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("acme/widgets", 42, "manual_issue", "success", "{}"),
        )
        conn.commit()

    with TestClient(app) as client:
        response = client.get("/runs/1")

    assert response.status_code == 200
    assert "https://github.com/acme/widgets/pull/42" not in response.text
    assert 'id="run-source-link">' in response.text


def test_manual_text_run_detail_omits_fake_pull_request_link(tmp_path: Path) -> None:
    db_path = _setup_db(tmp_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO autofix_runs (repo, pr_number, trigger_source, status, normalized_review_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("acme/widgets", 314159, "manual_task", "success", "{}"),
        )
        conn.commit()

    with TestClient(app) as client:
        response = client.get("/runs/1")

    assert response.status_code == 200
    assert "https://github.com/acme/widgets/pull/314159" not in response.text


def test_manual_issue_run_prefers_opened_pull_request_link(tmp_path: Path) -> None:
    db_path = _setup_db(tmp_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO autofix_runs (
                repo,
                pr_number,
                opened_pr_number,
                opened_pr_url,
                trigger_source,
                status,
                normalized_review_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "acme/widgets",
                42,
                99,
                "https://github.com/acme/widgets/pull/99",
                "manual_issue",
                "success",
                "{}",
            ),
        )
        conn.commit()

    with TestClient(app) as client:
        detail_response = client.get("/runs/1")
        index_response = client.get("/")

    assert detail_response.status_code == 200
    assert index_response.status_code == 200
    assert "https://github.com/acme/widgets/pull/99" in detail_response.text
    assert "https://github.com/acme/widgets/pull/99" in index_response.text
    assert "#99" in detail_response.text
    assert "#99" in index_response.text


def test_run_detail_uses_git_remote_provider_for_pull_request_link(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class _FakeRemoteProvider:
        def build_pull_request_url(self, *, repo: str, pr_number: int) -> str:
            return f"https://code.example/{repo}/merge/{pr_number}"

    monkeypatch.setattr(
        web_routes,
        "get_git_remote_provider",
        lambda: _FakeRemoteProvider(),
    )

    db_path = _setup_db(tmp_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO autofix_runs (repo, pr_number, trigger_source, status, normalized_review_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("acme/widgets", 42, "github_webhook", "success", "{}"),
        )
        conn.commit()

    with TestClient(app) as client:
        response = client.get("/runs/1")

    assert response.status_code == 200
    assert "https://code.example/acme/widgets/merge/42" in response.text
