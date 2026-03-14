from __future__ import annotations

import os
import sqlite3

from fastapi.testclient import TestClient

from app.config import get_settings
from app.db import init_db
from app.main import app


def _setup_db(tmp_path):
    get_settings.cache_clear()
    db_path = tmp_path / "software_factory.db"
    os.environ["DB_PATH"] = str(db_path)
    os.environ["MAX_AUTOFIX_PER_PR"] = "3"
    init_db()
    return db_path


def test_submit_issue_api_queues_autofix_run(tmp_path) -> None:
    db_path = _setup_db(tmp_path)

    payload = {
        "url": "https://github.com/acme/widgets/pull/42",
    }

    with TestClient(app) as client:
        response = client.post("/api/issues", json=payload)

    assert response.status_code == 200
    data = response.json()
    assert data["queue_status"] == "queued"
    assert isinstance(data["queued_run_id"], int)
    run_id = data["queued_run_id"]
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT trigger_source, normalized_review_json FROM autofix_runs WHERE id = ?",
            (run_id,),
        ).fetchone()

    assert row is not None
    assert str(row["trigger_source"]) == "manual_issue"


def test_submit_issue_api_duplicates_are_deduplicated(tmp_path) -> None:
    _setup_db(tmp_path)

    payload = {
        "url": "https://github.com/acme/widgets/pull/42",
    }

    with TestClient(app) as client:
        first = client.post("/api/issues", json=payload)
        second = client.post("/api/issues", json=payload)

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["queue_status"] == "queued"
    assert second.json()["queue_status"] == "duplicate_task"


def test_submit_issue_api_respects_autofix_limit(tmp_path) -> None:
    _setup_db(tmp_path)

    db_path = tmp_path / "software_factory.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO pull_requests (repo, pr_number, autofix_count)
            VALUES (?, ?, ?)
            """,
            ("acme/widgets", 42, 3),
        )
        conn.commit()

    payload = {
        "url": "https://github.com/acme/widgets/pull/42",
    }

    with TestClient(app) as client:
        response = client.post("/api/issues", json=payload)

    assert response.status_code == 200
    assert response.json()["queue_status"] == "autofix_limit_reached"


def test_submit_issue_api_supports_pull_and_issue_links(tmp_path, monkeypatch) -> None:
    _setup_db(tmp_path)

    monkeypatch.setattr(
        "app.routes.web._resolve_pr_number_from_issue",
        lambda _repo, _issue_number: 99,
    )

    payload = {"url": "https://github.com/acme/widgets/issues/99"}

    with TestClient(app) as client:
        response = client.post("/api/issues", json=payload)

    assert response.status_code == 200
    data = response.json()
    assert data["pr_number"] == 99
    assert data["issue_number"] == 99


def test_submit_issue_api_uses_non_pull_issue_as_task(tmp_path) -> None:
    _setup_db(tmp_path)

    payload = {"url": "https://github.com/acme/widgets/issues/99"}

    with TestClient(app) as client:
        response = client.post("/api/issues", json=payload)

    assert response.status_code == 200
    data = response.json()
    assert data["pr_number"] == 99
    assert data["issue_number"] == 99
