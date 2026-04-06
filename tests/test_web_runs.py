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
