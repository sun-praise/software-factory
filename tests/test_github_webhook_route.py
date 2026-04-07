from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app
from app.routes import github as github_route
from app.routes.github import _get_debounce_backend
from app.services.github_events import GitHubReviewEvent
from app.services.github_signature import (
    SignatureStatus,
    SignatureVerificationResult,
    build_signature,
)


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


def test_webhook_route_uses_webhook_provider(tmp_path: Path, monkeypatch) -> None:
    _set_env(tmp_path, secret="top-secret")
    calls: dict[str, object] = {}

    class _FakeWebhookProvider:
        @property
        def signature_header(self) -> str:
            return "X-Custom-Signature"

        def verify_signature(
            self,
            *,
            body: bytes,
            secret: str,
            signature_header: str | None,
        ) -> SignatureVerificationResult:
            calls["verify"] = {
                "body": body,
                "secret": secret,
                "signature_header": signature_header,
            }
            return SignatureVerificationResult(status=SignatureStatus.VERIFIED)

        def extract_review_event(self, *, event_type: str, payload: dict[str, object]):
            calls["event"] = {"event_type": event_type, "payload": payload}
            return GitHubReviewEvent(
                repo="acme/widgets",
                pr_number=42,
                event_type=event_type,
                event_id="1001",
                event_key="gh:pull_request_review:acme/widgets:42:1001",
                actor="reviewer",
                head_sha="abc123",
                raw_payload_json=json.dumps(payload, ensure_ascii=True, sort_keys=True),
            )

        def extract_event_body(self, *, event_type: str, payload: dict[str, object]):
            calls["body"] = {"event_type": event_type, "payload": payload}
            return "Please fix"

        def enrich_event_pull_request_info(
            self,
            *,
            event,
            payload,
            github_token: str,
        ):
            calls["enrich"] = {
                "event": event,
                "payload": payload,
                "github_token": github_token,
            }
            return event, payload

    monkeypatch.setattr(
        github_route,
        "get_webhook_provider",
        lambda: _FakeWebhookProvider(),
    )

    payload = {
        "repository": {"full_name": "acme/widgets"},
        "pull_request": {"number": 42, "head": {"sha": "abc123"}},
        "review": {"id": 1001, "body": "Please fix"},
        "sender": {"login": "reviewer"},
    }
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")

    with TestClient(app) as client:
        response = client.post(
            "/github/webhook",
            content=body,
            headers={
                "content-type": "application/json",
                "X-GitHub-Event": "pull_request_review",
                "X-Custom-Signature": "sha256=abc",
            },
        )

    assert response.status_code == 200
    assert response.json()["queue_status"] == "queued"
    assert isinstance(response.json()["queued_run_id"], int)
    assert calls["verify"] == {
        "body": body,
        "secret": "top-secret",
        "signature_header": "sha256=abc",
    }
    assert calls["event"] == {
        "event_type": "pull_request_review",
        "payload": payload,
    }
    assert calls["body"] == {
        "event_type": "pull_request_review",
        "payload": payload,
    }


def test_webhook_route_uses_provider_for_issue_comment_pr_info_enrichment(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _set_env(tmp_path, secret="")
    monkeypatch.setenv("GITHUB_TOKEN", "provider-token")
    calls: dict[str, object] = {}

    class _FakeWebhookProvider:
        @property
        def signature_header(self) -> str:
            return "X-Custom-Signature"

        def verify_signature(
            self,
            *,
            body: bytes,
            secret: str,
            signature_header: str | None,
        ) -> SignatureVerificationResult:
            return SignatureVerificationResult(status=SignatureStatus.VERIFIED)

        def extract_review_event(self, *, event_type: str, payload: dict[str, object]):
            return GitHubReviewEvent(
                repo="acme/widgets",
                pr_number=42,
                event_type=event_type,
                event_id="3001",
                event_key="gh:issue_comment:acme/widgets:42:3001",
                actor="reviewer",
                head_sha=None,
                raw_payload_json=json.dumps(payload, ensure_ascii=True, sort_keys=True),
            )

        def extract_event_body(self, *, event_type: str, payload: dict[str, object]):
            return "Please fix"

        def enrich_event_pull_request_info(
            self,
            *,
            event,
            payload,
            github_token: str,
        ):
            calls["enrich"] = {
                "event_type": event.event_type,
                "head_sha_before": event.head_sha,
                "github_token": github_token,
            }
            enriched_event = GitHubReviewEvent(
                repo=event.repo,
                pr_number=event.pr_number,
                event_type=event.event_type,
                event_id=event.event_id,
                event_key=event.event_key,
                actor=event.actor,
                head_sha="abc123",
                raw_payload_json=event.raw_payload_json,
            )
            enriched_payload = dict(payload)
            enriched_payload["pull_request"] = {
                "number": 42,
                "head": {"sha": "abc123", "ref": "feature/test"},
            }
            return enriched_event, enriched_payload

    monkeypatch.setattr(
        github_route,
        "get_webhook_provider",
        lambda: _FakeWebhookProvider(),
    )

    payload = {
        "repository": {"full_name": "acme/widgets"},
        "issue": {"number": 42, "pull_request": {"url": "https://example/pr/42"}},
        "comment": {"id": 3001, "body": "Please fix"},
        "sender": {"login": "reviewer"},
    }

    with TestClient(app) as client:
        response = client.post(
            "/github/webhook",
            json=payload,
            headers={
                "X-GitHub-Event": "issue_comment",
                "X-Custom-Signature": "sha256=abc",
            },
        )

    assert response.status_code == 200
    assert response.json()["queue_status"] == "queued"
    assert isinstance(response.json()["queued_run_id"], int)
    assert calls["enrich"] == {
        "event_type": "issue_comment",
        "head_sha_before": None,
        "github_token": "provider-token",
    }
    assert response.json()["idempotency_key"].startswith("task:acme/widgets:42:abc123:")


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


def test_autofix_summary_issue_comment_is_ignored(tmp_path: Path) -> None:
    _set_env(tmp_path, secret="")

    payload = {
        "repository": {"full_name": "acme/widgets"},
        "issue": {"number": 9, "pull_request": {"url": "https://example/pr/9"}},
        "comment": {
            "id": 3005,
            "body": "Autofix run #34\nStatus: success\nCommit: deadbeef",
        },
        "sender": {"login": "svtter"},
    }

    with TestClient(app) as client:
        response = client.post(
            "/github/webhook",
            json=payload,
            headers={"X-GitHub-Event": "issue_comment"},
        )

    assert response.status_code == 200
    assert response.json()["ignored"] is True
    assert response.json()["reason"] == "autofix_summary_comment"


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
    assert response.json().get("ignored") is not True
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


def test_check_run_event_is_recorded_without_queueing_and_enriches_next_run(
    tmp_path: Path,
) -> None:
    _set_env(tmp_path, secret="")
    db_path = tmp_path / "software_factory.db"

    check_payload = {
        "action": "completed",
        "repository": {"full_name": "acme/widgets", "language": "Python"},
        "check_run": {
            "id": 9001,
            "name": "CI / unit",
            "status": "completed",
            "conclusion": "failure",
            "details_url": "https://example.test/runs/9001",
            "head_sha": "abc123",
            "pull_requests": [{"number": 42}],
        },
        "sender": {"login": "github-actions[bot]"},
    }
    review_payload = {
        "repository": {"full_name": "acme/widgets", "language": "Python"},
        "pull_request": {"number": 42, "head": {"sha": "abc123", "ref": "feature/x"}},
        "review": {"id": 1001, "body": "Please fix the failing tests"},
        "sender": {"login": "reviewer"},
    }

    with TestClient(app) as client:
        check_response = client.post(
            "/github/webhook",
            json=check_payload,
            headers={"X-GitHub-Event": "check_run"},
        )
        review_response = client.post(
            "/github/webhook",
            json=review_payload,
            headers={"X-GitHub-Event": "pull_request_review"},
        )

    assert check_response.status_code == 200
    assert check_response.json()["insert_status"] == "inserted"
    assert check_response.json()["queue_status"] == "recorded"
    assert check_response.json()["queued_run_id"] is None

    assert review_response.status_code == 200
    assert review_response.json()["queue_status"] == "queued"
    run_id = review_response.json()["queued_run_id"]
    assert isinstance(run_id, int)

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT normalized_review_json FROM autofix_runs WHERE id = ?",
            (run_id,),
        ).fetchone()

    assert row is not None
    payload = json.loads(row["normalized_review_json"])
    assert payload["ci_status"] == "failed"
    assert payload["ci_checks"] == [
        {
            "source": "check_run",
            "name": "CI / unit",
            "status": "completed",
            "conclusion": "failure",
            "details_url": "https://example.test/runs/9001",
            "head_sha": "abc123",
        }
    ]


def test_webhook_uses_db_backed_runtime_settings(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    _get_debounce_backend.cache_clear()
    db_path = tmp_path / "software_factory.db"

    import os

    os.environ["DB_PATH"] = str(db_path)
    os.environ["GITHUB_WEBHOOK_SECRET"] = ""
    os.environ.pop("GITHUB_WEBHOOK_DEBOUNCE_SECONDS", None)
    os.environ.pop("MAX_RETRY_ATTEMPTS", None)
    os.environ.pop("MANAGED_REPO_PREFIXES", None)

    payload = {
        "repository": {"full_name": "acme/widgets"},
        "pull_request": {"number": 42, "head": {"sha": "abc123"}},
        "review": {"id": 2001, "body": "Please fix this"},
        "sender": {"login": "reviewer"},
    }

    with TestClient(app) as client:
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "INSERT INTO app_feature_flags (key, value) VALUES (?, ?)",
                ("runtime.github_webhook_debounce_seconds", "42"),
            )
            conn.execute(
                "INSERT INTO app_feature_flags (key, value) VALUES (?, ?)",
                ("runtime.max_retry_attempts", "7"),
            )
            conn.execute(
                "INSERT INTO app_feature_flags (key, value) VALUES (?, ?)",
                ("runtime.managed_repo_prefixes", '["acme/"]'),
            )
            conn.commit()

        response = client.post(
            "/github/webhook",
            json=payload,
            headers={"X-GitHub-Event": "pull_request_review"},
        )

    assert response.status_code == 200
    assert response.json()["queue_status"] == "queued"
    assert response.json()["debounce_window_seconds"] == 42.0
    run_id = response.json()["queued_run_id"]
    assert isinstance(run_id, int)

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT max_attempts FROM autofix_runs WHERE id = ?",
            (run_id,),
        ).fetchone()

    assert row is not None
    assert int(row["max_attempts"]) == 7
