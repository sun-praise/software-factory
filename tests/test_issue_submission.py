from __future__ import annotations

import json
from json import JSONDecodeError
import os
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.db import init_db
from app.main import app
from app.routes import web


def _setup_db(tmp_path):
    get_settings.cache_clear()
    db_path = tmp_path / "software_factory.db"
    os.environ["DB_PATH"] = str(db_path)
    os.environ["MAX_AUTOFIX_PER_PR"] = "3"
    init_db()
    return db_path


def test_submit_issue_api_queues_autofix_run(tmp_path, monkeypatch) -> None:
    db_path = _setup_db(tmp_path)

    monkeypatch.setattr(
        web,
        "_resolve_pr_number_from_issue",
        lambda *, owner, repo_name, issue_number: None,
    )
    monkeypatch.setattr(
        web,
        "_resolve_manual_issue_context",
        lambda target, description_present: web.ManualIssueContext(
            text="GitHub issue context\nTitle: Broken issue\nPlease fix it.",
            source_url=target.source_url,
        ),
    )

    payload = {
        "url": "https://github.com/acme/widgets/issues/42",
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
    normalized = json.loads(str(row["normalized_review_json"]))
    assert normalized["source_kind"] == "issue"
    assert normalized["resolved_pr_number"] is None


def test_submit_issue_api_queues_pull_request_feedback_from_pr_url(
    tmp_path,
    monkeypatch,
) -> None:
    db_path = _setup_db(tmp_path)

    monkeypatch.setattr(
        web,
        "_fetch_pull_request_feedback_review",
        lambda target, *, project_root=None: {
            "repo": target.repo,
            "pr_number": target.pr_number,
            "head_sha": None,
            "must_fix": [
                {
                    "source": "pull_request_review_comment",
                    "path": "app/routes/web.py",
                    "line": 42,
                    "text": "Fix the review finding",
                    "severity": "P1",
                }
            ],
            "should_fix": [],
            "ignore": [],
            "summary": "1 blocking issues, 0 suggestions, 0 ignored",
            "project_type": "python",
            "project_root": project_root,
            "issue_number": None,
            "manual_issue_source_url": target.source_url,
        },
    )

    with TestClient(app) as client:
        response = client.post(
            "/api/issues",
            json={"url": "https://github.com/acme/widgets/pull/42"},
        )

    assert response.status_code == 200
    run_id = response.json()["queued_run_id"]
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT normalized_review_json FROM autofix_runs WHERE id = ?",
            (run_id,),
        ).fetchone()

    assert row is not None
    normalized = json.loads(str(row["normalized_review_json"]))
    assert normalized["must_fix"][0]["text"] == "Fix the review finding"
    assert normalized["must_fix"][0]["path"] == "app/routes/web.py"
    assert normalized["source_kind"] == "pull"
    assert normalized["resolved_pr_number"] == 42


def test_submit_issue_api_duplicates_are_deduplicated(tmp_path) -> None:
    _setup_db(tmp_path)

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        web,
        "_fetch_pull_request_feedback_review",
        lambda target, *, project_root=None: {
            "repo": target.repo,
            "pr_number": target.pr_number,
            "head_sha": None,
            "must_fix": [
                {
                    "source": "pull_request_review_comment",
                    "path": "app/routes/web.py",
                    "line": 42,
                    "text": "Fix the review finding",
                    "severity": "P1",
                }
            ],
            "should_fix": [],
            "ignore": [],
            "summary": "1 blocking issues, 0 suggestions, 0 ignored",
            "project_type": "python",
            "project_root": project_root,
            "issue_number": None,
            "manual_issue_source_url": target.source_url,
        },
    )

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
    monkeypatch.undo()


def test_submit_issue_api_respects_autofix_limit(tmp_path) -> None:
    _setup_db(tmp_path)

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        web,
        "_fetch_pull_request_feedback_review",
        lambda target, *, project_root=None: {
            "repo": target.repo,
            "pr_number": target.pr_number,
            "head_sha": None,
            "must_fix": [
                {
                    "source": "pull_request_review_comment",
                    "path": None,
                    "line": None,
                    "text": "Fix",
                    "severity": "P1",
                }
            ],
            "should_fix": [],
            "ignore": [],
            "summary": "1 blocking issues, 0 suggestions, 0 ignored",
            "project_type": "python",
            "project_root": project_root,
            "issue_number": None,
            "manual_issue_source_url": target.source_url,
        },
    )

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
    monkeypatch.undo()


def test_submit_issue_api_rejects_invalid_links(tmp_path) -> None:
    _setup_db(tmp_path)

    payload = {"url": "https://github.com/acme/widgets/commit/abcdef"}

    with TestClient(app) as client:
        response = client.post("/api/issues", json=payload)

    assert response.status_code == 400


def test_submit_issue_api_accepts_issue_links(tmp_path, monkeypatch) -> None:
    db_path = _setup_db(tmp_path)

    monkeypatch.setattr(
        web,
        "_resolve_pr_number_from_issue",
        lambda *, owner, repo_name, issue_number: None,
    )
    monkeypatch.setattr(
        web,
        "_resolve_manual_issue_context",
        lambda target, description_present: web.ManualIssueContext(
            text="GitHub issue context\nTitle: Broken issue\nPlease fix it.",
            source_url=target.source_url,
        ),
    )

    payload = {"url": "https://github.com/acme/widgets/issues/99"}

    with TestClient(app) as client:
        response = client.post("/api/issues", json=payload)

    assert response.status_code == 200
    data = response.json()
    assert data["queue_status"] == "queued"
    assert data["pr_number"] == 99
    assert data["issue_number"] == 99

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT trigger_source, normalized_review_json FROM autofix_runs ORDER BY id DESC LIMIT 1",
        ).fetchone()

    assert row is not None
    assert str(row["trigger_source"]) == "manual_issue"
    normalized = json.loads(str(row["normalized_review_json"]))
    assert normalized["must_fix"][0]["context_resolved"] is True


def test_submit_issue_api_uses_issue_pr_number_for_pull_request_issues(
    tmp_path, monkeypatch
) -> None:
    _setup_db(tmp_path)

    monkeypatch.setattr(
        web,
        "_resolve_pr_number_from_issue",
        lambda *, owner, repo_name, issue_number: 88,
    )
    monkeypatch.setattr(
        web,
        "_resolve_manual_issue_context",
        lambda target, description_present: web.ManualIssueContext(
            text="GitHub issue context\nPlease fix it.",
            source_url=target.source_url,
        ),
    )

    payload = {"url": "https://github.com/acme/widgets/issues/99"}

    with TestClient(app) as client:
        response = client.post("/api/issues", json=payload)

    assert response.status_code == 200
    data = response.json()
    assert data["pr_number"] == 88
    assert data["issue_number"] == 99

    with sqlite3.connect(tmp_path / "software_factory.db") as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT normalized_review_json FROM autofix_runs ORDER BY id DESC LIMIT 1",
        ).fetchone()

    assert row is not None
    normalized = json.loads(str(row["normalized_review_json"]))
    assert normalized["source_kind"] == "issue"
    assert normalized["resolved_pr_number"] == 88


def test_submit_issue_api_rejects_pull_request_url_without_actionable_feedback(
    tmp_path,
    monkeypatch,
) -> None:
    _setup_db(tmp_path)

    monkeypatch.setattr(
        web,
        "_fetch_pull_request_feedback_review",
        lambda target, *, project_root=None: {
            "repo": target.repo,
            "pr_number": target.pr_number,
            "head_sha": None,
            "must_fix": [],
            "should_fix": [],
            "ignore": [],
            "summary": "0 blocking issues, 0 suggestions, 0 ignored",
            "project_type": "python",
            "project_root": project_root,
            "issue_number": None,
            "manual_issue_source_url": target.source_url,
        },
    )

    with TestClient(app) as client:
        response = client.post(
            "/api/issues",
            json={"url": "https://github.com/acme/widgets/pull/42"},
        )

    assert response.status_code == 400
    assert "No actionable pull request comments" in response.json()["detail"]


def test_resolve_pr_number_from_issue_rejects_invalid_json(monkeypatch) -> None:
    class _Response:
        status_code = 200

        def json(self):
            raise JSONDecodeError("bad json", "", 0)

    monkeypatch.setattr(web.httpx, "get", lambda *args, **kwargs: _Response())

    with pytest.raises(ValueError, match="invalid JSON"):
        web._resolve_pr_number_from_issue(
            owner="acme",
            repo_name="widgets",
            issue_number=99,
        )


def test_resolve_pr_number_from_issue_returns_none_for_invalid_pull_request_url(
    monkeypatch,
) -> None:
    class _Response:
        status_code = 200

        def json(self):
            return {
                "pull_request": {
                    "url": "https://api.github.com/repos/acme/widgets/pulls/not-a-number"
                }
            }

    monkeypatch.setattr(web.httpx, "get", lambda *args, **kwargs: _Response())

    assert (
        web._resolve_pr_number_from_issue(
            owner="acme",
            repo_name="widgets",
            issue_number=99,
        )
        is None
    )


def test_submit_issue_api_dry_run_validates_without_creating_run(
    tmp_path, monkeypatch
) -> None:
    _setup_db(tmp_path)

    monkeypatch.setattr(
        web,
        "_resolve_pr_number_from_issue",
        lambda *, owner, repo_name, issue_number: None,
    )
    monkeypatch.setattr(
        web,
        "_resolve_manual_issue_context",
        lambda target, description_present: web.ManualIssueContext(
            text="GitHub issue context\nTitle: Test\nBody: Test.",
            source_url=target.source_url,
        ),
    )

    payload = {
        "url": "https://github.com/acme/widgets/issues/42",
        "dry_run": True,
    }

    with TestClient(app) as client:
        response = client.post("/api/issues", json=payload)

    assert response.status_code == 200
    data = response.json()
    assert data["queue_status"] == "validated"
    assert data["queued_run_id"] is None


def test_submit_issue_api_reuses_existing_active_run_same_source_url(
    tmp_path, monkeypatch
) -> None:
    db_path = _setup_db(tmp_path)

    monkeypatch.setattr(
        web,
        "_resolve_pr_number_from_issue",
        lambda *, owner, repo_name, issue_number: None,
    )
    monkeypatch.setattr(
        web,
        "_resolve_manual_issue_context",
        lambda target, description_present: web.ManualIssueContext(
            text="GitHub issue context\nTitle: Test\nBody: Test.",
            source_url=target.source_url,
        ),
    )

    source_url = "https://github.com/acme/widgets/issues/42"

    with TestClient(app) as client:
        first = client.post("/api/issues", json={"url": source_url})
        assert first.status_code == 200
        first_data = first.json()
        assert first_data["queue_status"] == "queued"
        first_run_id = first_data["queued_run_id"]

        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "UPDATE autofix_runs SET status = 'running' WHERE id = ?",
                (first_run_id,),
            )
            conn.commit()

        second = client.post("/api/issues", json={"url": source_url})
        assert second.status_code == 200
        second_data = second.json()
        assert second_data["queue_status"] == "reused_active_run"
        assert second_data["queued_run_id"] == first_run_id
        assert second_data["existing_run_id"] == first_run_id
        assert second_data["existing_run_status"] == "running"


def test_submit_issue_api_creates_new_run_after_previous_stops(
    tmp_path, monkeypatch
) -> None:
    db_path = _setup_db(tmp_path)

    monkeypatch.setattr(
        web,
        "_resolve_pr_number_from_issue",
        lambda *, owner, repo_name, issue_number: None,
    )
    monkeypatch.setattr(
        web,
        "_resolve_manual_issue_context",
        lambda target, description_present: web.ManualIssueContext(
            text="GitHub issue context\nTitle: Test\nBody: Test.",
            source_url=target.source_url,
        ),
    )

    source_url = "https://github.com/acme/widgets/issues/42"

    with TestClient(app) as client:
        first = client.post("/api/issues", json={"url": source_url})
        assert first.status_code == 200
        first_data = first.json()
        assert first_data["queue_status"] == "queued"
        first_run_id = first_data["queued_run_id"]

        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "UPDATE autofix_runs SET status = 'failed' WHERE id = ?",
                (first_run_id,),
            )
            conn.commit()

        second = client.post("/api/issues", json={"url": source_url})
        assert second.status_code == 200
        second_data = second.json()
        assert second_data["queue_status"] == "queued"
        assert second_data["queued_run_id"] != first_run_id


def test_submit_issue_batch_csv_endpoint(tmp_path, monkeypatch) -> None:
    _setup_db(tmp_path)

    monkeypatch.setattr(
        web,
        "_resolve_pr_number_from_issue",
        lambda *, owner, repo_name, issue_number: None,
    )
    monkeypatch.setattr(
        web,
        "_resolve_manual_issue_context",
        lambda target, description_present: web.ManualIssueContext(
            text="GitHub issue context\nTitle: Test\nBody: Test.",
            source_url=target.source_url,
        ),
    )

    csv_content = "url,description\nhttps://github.com/acme/widgets/issues/42,Fix bug\nhttps://github.com/acme/widgets/issues/43,Fix another"

    with TestClient(app) as client:
        response = client.post(
            "/api/issues/batch",
            files={"file": ("issues.csv", csv_content, "text/csv")},
        )

    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert data["summary"]["created"] == 2
    assert len(data["results"]) == 2


def test_submit_issue_batch_csv_validates_required_columns(tmp_path) -> None:
    _setup_db(tmp_path)

    csv_content = "description\nJust a description"

    with TestClient(app) as client:
        response = client.post(
            "/api/issues/batch",
            files={"file": ("issues.csv", csv_content, "text/csv")},
        )

    assert response.status_code == 400
    assert "required columns" in response.json()["detail"].lower()


def test_submit_issue_api_propagates_project_root(tmp_path, monkeypatch) -> None:
    db_path = _setup_db(tmp_path)

    monkeypatch.setattr(
        web,
        "_resolve_pr_number_from_issue",
        lambda *, owner, repo_name, issue_number: None,
    )
    monkeypatch.setattr(
        web,
        "_resolve_manual_issue_context",
        lambda target, description_present: web.ManualIssueContext(
            text="GitHub issue context\nTitle: Broken issue\nPlease fix it.",
            source_url=target.source_url,
        ),
    )

    payload = {
        "url": "https://github.com/acme/widgets/issues/42",
        "project_root": "latex-agent",
    }

    with TestClient(app) as client:
        response = client.post("/api/issues", json=payload)

    assert response.status_code == 200
    data = response.json()
    assert data["queue_status"] == "queued"
    run_id = data["queued_run_id"]

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT normalized_review_json FROM autofix_runs WHERE id = ?",
            (run_id,),
        ).fetchone()

    assert row is not None
    normalized = json.loads(str(row["normalized_review_json"]))
    assert normalized["project_root"] == "latex-agent"


def test_submit_issue_api_omits_project_root_when_not_provided(
    tmp_path, monkeypatch
) -> None:
    db_path = _setup_db(tmp_path)

    monkeypatch.setattr(
        web,
        "_resolve_pr_number_from_issue",
        lambda *, owner, repo_name, issue_number: None,
    )
    monkeypatch.setattr(
        web,
        "_resolve_manual_issue_context",
        lambda target, description_present: web.ManualIssueContext(
            text="GitHub issue context\nTitle: Broken issue\nPlease fix it.",
            source_url=target.source_url,
        ),
    )

    payload = {
        "url": "https://github.com/acme/widgets/issues/42",
    }

    with TestClient(app) as client:
        response = client.post("/api/issues", json=payload)

    assert response.status_code == 200
    run_id = response.json()["queued_run_id"]

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT normalized_review_json FROM autofix_runs WHERE id = ?",
            (run_id,),
        ).fetchone()

    assert row is not None
    normalized = json.loads(str(row["normalized_review_json"]))
    assert normalized.get("project_root") is None


def test_submit_issue_batch_csv_propagates_project_root(tmp_path, monkeypatch) -> None:
    _setup_db(tmp_path)

    monkeypatch.setattr(
        web,
        "_resolve_pr_number_from_issue",
        lambda *, owner, repo_name, issue_number: None,
    )
    monkeypatch.setattr(
        web,
        "_resolve_manual_issue_context",
        lambda target, description_present: web.ManualIssueContext(
            text="GitHub issue context\nTitle: Test\nBody: Test.",
            source_url=target.source_url,
        ),
    )

    csv_content = (
        "url,description,project_root\n"
        "https://github.com/acme/widgets/issues/42,Fix bug,backend\n"
        "https://github.com/acme/widgets/issues/43,Fix another,"
    )

    with TestClient(app) as client:
        response = client.post(
            "/api/issues/batch",
            files={"file": ("issues.csv", csv_content, "text/csv")},
        )

    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert data["summary"]["created"] == 2
