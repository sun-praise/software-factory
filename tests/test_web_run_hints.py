from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import get_settings
from app.db import init_db
from app.main import app


def _setup_db(tmp_path: Path) -> Path:
    get_settings.cache_clear()
    db_path = tmp_path / "software_factory.db"
    os.environ["DB_PATH"] = str(db_path)
    init_db()
    return db_path


def _insert_run(db_path: Path, *, status: str) -> int:
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO autofix_runs (
                repo,
                pr_number,
                head_sha,
                status,
                normalized_review_json
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                "acme/widgets",
                7,
                "abc123",
                status,
                "{}",
            ),
        )
        conn.commit()
        assert cursor.lastrowid is not None
        return int(cursor.lastrowid)


def test_append_run_operator_hint_api_updates_active_run(tmp_path: Path) -> None:
    db_path = _setup_db(tmp_path)
    run_id = _insert_run(db_path, status="running")

    with TestClient(app) as client:
        response = client.post(
            f"/api/runs/{run_id}/operator-hints",
            data={"text": "Only touch app/services/filter.py"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["operator_hints"] == "Only touch app/services/filter.py"
    assert payload["operator_hints_editable"] == "true"


def test_append_run_operator_hint_api_rejects_finished_run(tmp_path: Path) -> None:
    db_path = _setup_db(tmp_path)
    run_id = _insert_run(db_path, status="success")

    with TestClient(app) as client:
        response = client.post(
            f"/api/runs/{run_id}/operator-hints",
            data={"text": "Do not change public API"},
        )

    assert response.status_code == 409
