from __future__ import annotations

import json
import sqlite3
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
    os.environ["MAX_AUTOFIX_PER_PR"] = "3"
    os.environ["MAX_RETRY_ATTEMPTS"] = "3"
    os.environ["BOT_LOGINS"] = "github-actions[bot],dependabot[bot]"
    os.environ["NOISE_COMMENT_PATTERNS"] = r"^/retest\b,^/resolve\b"
    os.environ["MANAGED_REPO_PREFIXES"] = "acme/"


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
    assert first.json()["queue_status"] == "queued"
    assert first.json()["event_key"] == "gh:pull_request_review:acme/widgets:42:1001"
    assert first.json()["idempotency_key"].startswith("task:acme/widgets:42:abc123:")
    assert isinstance(first.json()["queued_run_id"], int)

    assert second.status_code == 200
    assert second.json()["insert_status"] == "duplicate"
    assert second.json()["queue_status"] == "duplicate_event"
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


def test_bot_comment_is_filtered(tmp_path: Path) -> None:
    _set_env(tmp_path, secret="")

    payload = {
        "repository": {"full_name": "acme/widgets"},
        "issue": {"number": 9, "pull_request": {"url": "https://example/pr/9"}},
        "comment": {"id": 3004, "body": "please re-run"},
        "sender": {"login": "dependabot[bot]"},
    }

    with TestClient(app) as client:
        response = client.post(
            "/github/webhook",
            json=payload,
            headers={"X-GitHub-Event": "issue_comment"},
        )

    assert response.status_code == 200
    assert response.json()["ignored"] is True
    assert response.json()["reason"] == "noise_actor"


def test_bot_pull_request_review_is_queued(tmp_path: Path) -> None:
    _set_env(tmp_path, secret="")

    payload = {
        "repository": {"full_name": "acme/widgets"},
        "pull_request": {"number": 42, "head": {"sha": "abc123"}},
        "review": {"id": 1003, "body": "Please fix this"},
        "sender": {"login": "github-actions[bot]"},
    }

    with TestClient(app) as client:
        response = client.post(
            "/github/webhook",
            json=payload,
            headers={"X-GitHub-Event": "pull_request_review"},
        )

    assert response.status_code == 200
    assert response.json()["ignored"] is not True
    assert response.json()["insert_status"] == "inserted"
    assert response.json()["queue_status"] == "queued"
    assert isinstance(response.json()["queued_run_id"], int)


def test_autofix_limit_prevents_queueing_new_run(tmp_path: Path) -> None:
    _set_env(tmp_path, secret="")
    db_path = tmp_path / "software_factory.db"

    with TestClient(app) as client:
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
            "repository": {"full_name": "acme/widgets"},
            "pull_request": {"number": 42, "head": {"sha": "abc123"}},
            "review": {"id": 1002, "body": "Please fix this"},
            "sender": {"login": "reviewer"},
        }
        response = client.post(
            "/github/webhook",
            json=payload,
            headers={"X-GitHub-Event": "pull_request_review"},
        )

    assert response.status_code == 200
    assert response.json()["insert_status"] == "inserted"
    assert response.json()["queue_status"] == "autofix_limit_reached"
    assert response.json()["queued_run_id"] is None
