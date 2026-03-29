from __future__ import annotations

import json
import os
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.db import init_db
from app.main import app
from app.services.issue_input import (
    GitHubIssueProvider,
    PlainTextProvider,
    TaskInput,
    build_normalized_review_from_task_input,
    get_registered_providers,
    parse_task_input,
    parse_task_input_with_provider,
    register_provider,
)
from app.services.agent_prompt import _is_task_input_run


def _setup_db(tmp_path):
    get_settings.cache_clear()
    db_path = tmp_path / "software_factory.db"
    os.environ["DB_PATH"] = str(db_path)
    os.environ["MAX_AUTOFIX_PER_PR"] = "3"
    init_db()
    return db_path


class TestTaskInputModel:
    def test_display_label_with_source_url(self):
        task = TaskInput(
            title="Fix bug",
            body="Fix it now",
            provider="github",
            source_url="https://github.com/owner/repo/issues/1",
        )
        assert task.display_label == "https://github.com/owner/repo/issues/1"

    def test_display_label_with_source_id(self):
        task = TaskInput(
            title="Fix bug",
            body="Fix it now",
            provider="github",
            source_id="owner/repo/issues/1",
        )
        assert task.display_label == "github#owner/repo/issues/1"

    def test_display_label_fallback(self):
        task = TaskInput(
            title="Fix bug",
            body="Fix it now",
            provider="plain_text",
        )
        assert task.display_label == "plain_text: Fix bug"


class TestGitHubIssueProvider:
    def setup_method(self):
        self.provider = GitHubIssueProvider()

    def test_can_handle_github_issue_url(self):
        assert self.provider.can_handle("https://github.com/owner/repo/issues/42")

    def test_can_handle_github_pr_url(self):
        assert self.provider.can_handle("https://github.com/owner/repo/pull/42")

    def test_can_handle_github_pulls_url(self):
        assert self.provider.can_handle("https://github.com/owner/repo/pulls/42")

    def test_cannot_handle_plain_text(self):
        assert not self.provider.can_handle("Fix this bug")

    def test_cannot_handle_non_github_url(self):
        assert not self.provider.can_handle("https://gitlab.com/owner/repo/issues/1")

    def test_cannot_handle_empty(self):
        assert not self.provider.can_handle("")

    def test_parse_issue_url(self):
        result = self.provider.parse("https://github.com/acme/widgets/issues/99")
        assert result.provider == "github"
        assert result.source_url == "https://github.com/acme/widgets/issues/99"
        assert result.metadata["repo"] == "acme/widgets"
        assert result.metadata["number"] == 99
        assert result.metadata["url_kind"] == "issue"

    def test_parse_pr_url(self):
        result = self.provider.parse("https://github.com/acme/widgets/pull/42")
        assert result.provider == "github"
        assert result.metadata["url_kind"] == "pull"
        assert result.metadata["number"] == 42

    def test_parse_invalid_url_raises(self):
        with pytest.raises(ValueError, match="Invalid GitHub URL"):
            self.provider.parse("https://github.com/owner/repo")

    def test_parse_non_numeric_number_raises(self):
        with pytest.raises(ValueError, match="numeric"):
            self.provider.parse("https://github.com/owner/repo/issues/abc")


class TestPlainTextProvider:
    def setup_method(self):
        self.provider = PlainTextProvider()

    def test_can_handle_non_empty_text(self):
        assert self.provider.can_handle("Fix this bug")

    def test_cannot_handle_empty(self):
        assert not self.provider.can_handle("")

    def test_parse_single_line(self):
        result = self.provider.parse("Fix this bug")
        assert result.provider == "plain_text"
        assert result.title == "Fix this bug"
        assert result.body == "Fix this bug"

    def test_parse_multiline(self):
        text = "Fix the login bug\n\nUsers cannot log in when the token expires."
        result = self.provider.parse(text)
        assert result.title == "Fix the login bug"
        assert "Users cannot log in" in result.body

    def test_parse_empty_raises(self):
        with pytest.raises(ValueError, match="must not be empty"):
            self.provider.parse("")


class TestParseTaskInput:
    def test_auto_detects_github_url(self):
        result = parse_task_input("https://github.com/acme/repo/issues/5")
        assert result.provider == "github"

    def test_falls_back_to_plain_text(self):
        result = parse_task_input("Fix the login page")
        assert result.provider == "plain_text"

    def test_rejects_empty(self):
        with pytest.raises(ValueError, match="must not be empty"):
            parse_task_input("")


class TestParseTaskInputWithProvider:
    def test_explicit_github_provider(self):
        result = parse_task_input_with_provider(
            "https://github.com/acme/repo/issues/5", "github"
        )
        assert result.provider == "github"

    def test_explicit_plain_text_provider(self):
        result = parse_task_input_with_provider("Fix something", "plain_text")
        assert result.provider == "plain_text"

    def test_unknown_provider_raises(self):
        with pytest.raises(ValueError, match="Unknown provider"):
            parse_task_input_with_provider("Fix something", "jira")

    def test_provider_cannot_handle_raises(self):
        with pytest.raises(ValueError, match="cannot handle"):
            parse_task_input_with_provider("Fix something", "github")

    def test_empty_raises(self):
        with pytest.raises(ValueError, match="must not be empty"):
            parse_task_input_with_provider("", "plain_text")


class TestRegisterProvider:
    def test_register_and_list(self):
        class CustomProvider:
            provider_name = "custom"

            def can_handle(self, x):
                return False

            def parse(self, x):
                return TaskInput(title="x", body="x", provider="custom")

        register_provider(CustomProvider())
        assert "custom" in get_registered_providers()


class TestBuildNormalizedReviewFromTaskInput:
    def test_github_task_input(self):
        task = GitHubIssueProvider().parse("https://github.com/acme/widgets/issues/42")
        review = build_normalized_review_from_task_input(task_input=task)
        assert review["repo"] == "acme/widgets"
        assert review["pr_number"] == 42
        assert review["task_input_provider"] == "github"
        assert len(review["must_fix"]) == 1
        assert review["must_fix"][0]["task_provider"] == "github"

    def test_plain_text_task_input(self):
        task = PlainTextProvider().parse("Fix the login bug\n\nDetails here")
        review = build_normalized_review_from_task_input(
            task_input=task, repo="acme/widgets"
        )
        assert review["repo"] == "acme/widgets"
        assert review["task_input_provider"] == "plain_text"
        assert "Fix the login bug" in review["must_fix"][0]["text"]

    def test_with_description(self):
        task = PlainTextProvider().parse("Fix bug")
        review = build_normalized_review_from_task_input(
            task_input=task, description="This is urgent"
        )
        assert "This is urgent" in review["must_fix"][0]["text"]

    def test_with_explicit_repo_and_pr(self):
        task = PlainTextProvider().parse("Fix bug")
        review = build_normalized_review_from_task_input(
            task_input=task, repo="acme/app", pr_number=10
        )
        assert review["repo"] == "acme/app"
        assert review["pr_number"] == 10


class TestIsTaskInputRun:
    def test_detects_task_input_run(self):
        assert _is_task_input_run({"task_input_provider": "plain_text"})

    def test_does_not_detect_normal_run(self):
        assert not _is_task_input_run({"source_kind": "pull"})

    def test_does_not_detect_github_issue_run(self):
        assert not _is_task_input_run({"source_kind": "issue"})


class TestTaskSubmissionAPI:
    def test_submit_plain_text_task_api(self, tmp_path) -> None:
        _setup_db(tmp_path)

        payload = {
            "input": "Fix the login bug\n\nUsers cannot authenticate after token expiry.",
            "repo": "acme/widgets",
        }

        with TestClient(app) as client:
            response = client.post("/api/tasks", json=payload)

        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is True
        assert data["queue_status"] == "queued"
        assert data["provider"] == "plain_text"
        assert data["task_title"] == "Fix the login bug"
        assert isinstance(data["queued_run_id"], int)

    def test_submit_github_url_via_task_api(self, tmp_path) -> None:
        _setup_db(tmp_path)

        payload = {
            "input": "https://github.com/acme/widgets/issues/42",
        }

        with TestClient(app) as client:
            response = client.post("/api/tasks", json=payload)

        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is True
        assert data["provider"] == "github"
        assert data["repo"] == "acme/widgets"

    def test_submit_task_dry_run(self, tmp_path) -> None:
        _setup_db(tmp_path)

        payload = {
            "input": "Fix bug",
            "dry_run": True,
        }

        with TestClient(app) as client:
            response = client.post("/api/tasks", json=payload)

        assert response.status_code == 200
        data = response.json()
        assert data["queue_status"] == "validated"
        assert data["queued_run_id"] is None

    def test_submit_task_rejects_empty(self, tmp_path) -> None:
        _setup_db(tmp_path)

        payload = {"input": ""}

        with TestClient(app) as client:
            response = client.post("/api/tasks", json=payload)

        assert response.status_code == 422

    def test_submit_task_explicit_provider(self, tmp_path) -> None:
        _setup_db(tmp_path)

        payload = {
            "input": "Fix the bug",
            "provider": "plain_text",
        }

        with TestClient(app) as client:
            response = client.post("/api/tasks", json=payload)

        assert response.status_code == 200
        assert response.json()["provider"] == "plain_text"

    def test_submit_task_unknown_provider_rejected(self, tmp_path) -> None:
        _setup_db(tmp_path)

        payload = {
            "input": "Fix the bug",
            "provider": "jira",
        }

        with TestClient(app) as client:
            response = client.post("/api/tasks", json=payload)

        assert response.status_code == 400
        assert "Unknown provider" in response.json()["detail"]

    def test_task_web_page_renders(self, tmp_path) -> None:
        _setup_db(tmp_path)

        with TestClient(app) as client:
            response = client.get("/tasks")

        assert response.status_code == 200
        assert "Submit Task" in response.text

    def test_task_persisted_to_db_with_correct_trigger_source(self, tmp_path) -> None:
        db_path = _setup_db(tmp_path)

        payload = {
            "input": "Fix the API endpoint",
            "repo": "acme/api",
        }

        with TestClient(app) as client:
            response = client.post("/api/tasks", json=payload)

        assert response.status_code == 200
        run_id = response.json()["queued_run_id"]

        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT trigger_source, normalized_review_json FROM autofix_runs WHERE id = ?",
                (run_id,),
            ).fetchone()

        assert row is not None
        assert str(row["trigger_source"]) == "task_input:plain_text"
        normalized = json.loads(str(row["normalized_review_json"]))
        assert normalized["task_input_provider"] == "plain_text"
