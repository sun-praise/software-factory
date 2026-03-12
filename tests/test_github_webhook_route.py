from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app
from app.routes.github import _get_debounce_backend
from app.services.github_signature import build_signature


def _set_env(tmp_path: Path, secret: str) -> None:
    get_settings.cache_clear()
    _get_debounce_backend.cache_clear()
    db_path = tmp_path / "software_factory.db"
    import os

    os.environ["DB_PATH"] = str(db_path)
    os.environ["GITHUB_WEBHOOK_SECRET"] = secret
    os.environ["GITHUB_WEBHOOK_DEBOUNCE_SECONDS"] = "60"


def test_webhook_rejects_invalid_signature(tmp_path: Path) -> None:
    _set_env(tmp_path, secret="top-secret")

    payload = {
        "repository": {"full_name": "acme/widgets"},
        "pull_request": {"number": 42, "head": {"sha": "abc123"}},
        "review": {"id": 1001},
        "sender": {"login": "reviewer"},
    }

    with TestClient(app) as client:
        response = client.post(
            "/github/webhook",
            json=payload,
            headers={
                "X-GitHub-Event": "pull_request_review",
                "X-Hub-Signature-256": "sha256=" + "0" * 64,
            },
        )

    assert response.status_code == 401


def test_webhook_inserts_and_deduplicates_review_event(tmp_path: Path) -> None:
    _set_env(tmp_path, secret="top-secret")

    payload = {
        "repository": {"full_name": "acme/widgets"},
        "pull_request": {"number": 42, "head": {"sha": "abc123"}},
        "review": {"id": 1001},
        "sender": {"login": "reviewer"},
    }
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    signature = "sha256=" + build_signature(body=body, secret="top-secret")

    with TestClient(app) as client:
        first = client.post(
            "/github/webhook",
            content=body,
            headers={
                "content-type": "application/json",
                "X-GitHub-Event": "pull_request_review",
                "X-Hub-Signature-256": signature,
            },
        )
        second = client.post(
            "/github/webhook",
            content=body,
            headers={
                "content-type": "application/json",
                "X-GitHub-Event": "pull_request_review",
                "X-Hub-Signature-256": signature,
            },
        )

    assert first.status_code == 200
    assert first.json()["insert_status"] == "inserted"
    assert first.json()["event_key"] == "gh:pull_request_review:acme/widgets:42:1001"
    assert isinstance(first.json()["queued_run_id"], int)

    assert second.status_code == 200
    assert second.json()["insert_status"] == "duplicate"
    assert second.json()["event_key"] == "gh:pull_request_review:acme/widgets:42:1001"
    assert second.json()["queued_run_id"] is None


def test_issue_comment_without_pr_is_ignored(tmp_path: Path) -> None:
    _set_env(tmp_path, secret="")

    payload = {
        "repository": {"full_name": "acme/widgets"},
        "issue": {"number": 9},
        "comment": {"id": 3004},
    }

    with TestClient(app) as client:
        response = client.post(
            "/github/webhook",
            json=payload,
            headers={"X-GitHub-Event": "issue_comment"},
        )

    assert response.status_code == 200
    assert response.json()["ignored"] is True
