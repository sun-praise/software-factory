from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app
from app.routes.github import _get_debounce_backend


def _set_env(tmp_path: Path, secret: str, monkeypatch: pytest.MonkeyPatch) -> None:
    get_settings.cache_clear()
    _get_debounce_backend.cache_clear()
    db_path = tmp_path / "software_factory.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("WEBHOOK_PROVIDER", "gitee")
    monkeypatch.setenv("GITEE_WEBHOOK_SECRET", secret)
    monkeypatch.setenv("GITEE_TOKEN", "gitee-token")
    monkeypatch.setenv("GITHUB_WEBHOOK_DEBOUNCE_SECONDS", "60")
    monkeypatch.setenv("MAX_AUTOFIX_PER_PR", "3")
    monkeypatch.setenv("MAX_RETRY_ATTEMPTS", "3")
    monkeypatch.setenv("MANAGED_REPO_PREFIXES", "acme/")


def test_gitee_webhook_note_hook_queues_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_env(tmp_path, secret="top-secret", monkeypatch=monkeypatch)

    payload = {
        "repository": {"path_with_namespace": "acme/widgets"},
        "noteable_type": "PullRequest",
        "comment": {
            "id": 3001,
            "body": "Please fix",
            "user": {"login": "reviewer"},
        },
        "pull_request": {
            "number": 42,
            "head": {"sha": "abc123", "ref": "feature/test"},
        },
        "sender": {"login": "reviewer"},
    }

    with TestClient(app) as client:
        response = client.post(
            "/github/webhook",
            json=payload,
            headers={
                "X-Gitee-Event": "Note Hook",
                "X-Gitee-Token": "top-secret",
            },
        )

    assert response.status_code == 200
    data = response.json()
    assert data["queue_status"] == "queued"
    assert data["repo"] == "acme/widgets"
    assert data["pr_number"] == 42
    assert isinstance(data["queued_run_id"], int)
