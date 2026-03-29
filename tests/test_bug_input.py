from __future__ import annotations

import json
import os
import sqlite3

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import get_settings
from app.db import init_db
from app.main import app
from app.schemas.bug_input import (
    BugContext,
    BugInput,
    BugProviderKind,
    BugSubmissionRequest,
    BugSubmissionResponse,
)
from app.services.bug_input import (
    BUG_INPUT_PROVIDERS,
    GitHubIssueBugProvider,
    GitHubPRBugProvider,
    LogStacktraceBugProvider,
    PlaintextBugProvider,
    StructuredBugProvider,
    build_bug_idempotency_key,
    register_provider,
    resolve_provider,
    _extract_error_messages,
    _extract_file_references,
    _extract_stacktraces,
)
from app.routes.bugs import _synthetic_pr_number


def _setup_db(tmp_path):
    get_settings.cache_clear()
    db_path = tmp_path / "software_factory.db"
    os.environ["DB_PATH"] = str(db_path)
    os.environ["MAX_AUTOFIX_PER_PR"] = "3"
    init_db()
    return db_path


class TestBugInputSchema:
    def test_plaintext_bug_input_defaults(self):
        bi = BugInput(title="Fix login crash")
        assert bi.provider == BugProviderKind.PLAINTEXT
        assert bi.description == ""
        assert bi.repo is None
        assert bi.source_url is None
        assert bi.context == BugContext()

    def test_structured_bug_input_with_context(self):
        ctx = BugContext(
            files=["src/auth.py"],
            error_messages=["NullPointerError at line 42"],
            stack_traces=["Traceback (most recent call last): ..."],
        )
        bi = BugInput(
            provider=BugProviderKind.STRUCTURED,
            title="Auth module crash",
            description="App crashes on login",
            repo="acme/web",
            context=ctx,
        )
        assert bi.provider == BugProviderKind.STRUCTURED
        assert len(bi.context.error_messages) == 1
        assert bi.context.files[0] == "src/auth.py"

    def test_submission_request_to_bug_input(self):
        req = BugSubmissionRequest(
            provider=BugProviderKind.LOG_STACKTRACE,
            title="OOM in worker",
            description="java.lang.OutOfMemoryError",
            repo="acme/service",
        )
        bi = req.to_bug_input()
        assert bi.provider == BugProviderKind.LOG_STACKTRACE
        assert bi.title == "OOM in worker"
        assert bi.context == BugContext()

    def test_submission_request_with_context(self):
        ctx = BugContext(error_messages=["Division by zero"])
        req = BugSubmissionRequest(
            title="Calc bug",
            description="Math error",
            context=ctx,
        )
        bi = req.to_bug_input()
        assert bi.context.error_messages == ["Division by zero"]

    def test_extra_fields_rejected(self):
        with pytest.raises(ValidationError):
            BugInput(title="test", unknown_field="value")

    def test_empty_title_rejected(self):
        with pytest.raises(ValidationError):
            BugInput(title="")


class TestPlaintextBugProvider:
    def test_supports_plaintext(self):
        provider = PlaintextBugProvider()
        bi = BugInput(title="test", provider=BugProviderKind.PLAINTEXT)
        assert provider.supports(bi)

    def test_rejects_non_plaintext(self):
        provider = PlaintextBugProvider()
        bi = BugInput(title="test", provider=BugProviderKind.GITHUB_PR)
        assert not provider.supports(bi)

    def test_to_normalized_review_basic(self):
        provider = PlaintextBugProvider()
        bi = BugInput(
            title="Login crash", description="App crashes when clicking login"
        )
        result = provider.to_normalized_review(
            bi, repo="acme/web", synthetic_pr_number=123
        )
        assert result["repo"] == "acme/web"
        assert result["pr_number"] == 123
        assert result["source_kind"] == "bug_input"
        assert result["bug_provider"] == "plaintext"
        assert len(result["must_fix"]) == 1
        assert "Login crash" in result["must_fix"][0]["text"]
        assert result["should_fix"] == []
        assert result["ignore"] == []

    def test_to_normalized_review_with_files_and_errors(self):
        provider = PlaintextBugProvider()
        ctx = BugContext(
            files=["src/login.py", "src/auth.py"],
            error_messages=["security vulnerability in auth", "critical crash"],
        )
        bi = BugInput(title="Auth bug", context=ctx)
        result = provider.to_normalized_review(
            bi, repo="acme/web", synthetic_pr_number=1
        )
        assert result["must_fix"][0]["severity"] == "P0"
        assert result["must_fix"][0]["path"] == "src/login.py"

    def test_to_normalized_review_with_source_url(self):
        provider = PlaintextBugProvider()
        bi = BugInput(title="External bug", source_url="https://jira.acme.com/BUG-42")
        result = provider.to_normalized_review(
            bi, repo="acme/web", synthetic_pr_number=1
        )
        assert result["bug_source_url"] == "https://jira.acme.com/BUG-42"


class TestStructuredBugProvider:
    def test_supports_structured(self):
        provider = StructuredBugProvider()
        bi = BugInput(title="test", provider=BugProviderKind.STRUCTURED)
        assert provider.supports(bi)

    def test_multiple_error_items(self):
        provider = StructuredBugProvider()
        ctx = BugContext(
            files=["a.py", "b.py"],
            error_messages=["Error in a.py", "Error in b.py"],
        )
        bi = BugInput(title="Multi error", context=ctx)
        result = provider.to_normalized_review(
            bi, repo="acme/web", synthetic_pr_number=1
        )
        assert len(result["must_fix"]) == 2
        assert result["must_fix"][0]["path"] == "a.py"
        assert result["must_fix"][1]["path"] == "b.py"
        assert result["summary"] == "2 blocking issues, 0 suggestions, 0 ignored"

    def test_empty_context_falls_back_to_title(self):
        provider = StructuredBugProvider()
        bi = BugInput(title="Fallback test")
        result = provider.to_normalized_review(
            bi, repo="acme/web", synthetic_pr_number=1
        )
        assert len(result["must_fix"]) == 1
        assert "Fallback test" in result["must_fix"][0]["text"]

    def test_more_stack_traces_than_files(self):
        provider = StructuredBugProvider()
        ctx = BugContext(
            files=["a.py", "b.py"],
            error_messages=["Error in a"],
            stack_traces=["Trace 1", "Trace 2", "Trace 3"],
        )
        bi = BugInput(title="Mismatch", context=ctx)
        result = provider.to_normalized_review(
            bi, repo="acme/web", synthetic_pr_number=1
        )
        assert len(result["must_fix"]) == 2
        assert result["must_fix"][0]["path"] == "a.py"
        assert result["must_fix"][1]["path"] == "b.py"


class TestLogStacktraceBugProvider:
    def test_supports_log_stacktrace(self):
        provider = LogStacktraceBugProvider()
        bi = BugInput(title="test", provider=BugProviderKind.LOG_STACKTRACE)
        assert provider.supports(bi)

    def test_extracts_errors_from_description(self):
        provider = LogStacktraceBugProvider()
        bi = BugInput(
            title="Runtime error",
            description="ERROR: NullPointerException\nERROR: Connection refused",
        )
        result = provider.to_normalized_review(
            bi, repo="acme/web", synthetic_pr_number=1
        )
        must_fix = result["must_fix"]
        assert len(must_fix) >= 1
        assert any("NullPointerException" in item["text"] for item in must_fix)

    def test_uses_context_logs_when_no_description(self):
        provider = LogStacktraceBugProvider()
        ctx = BugContext(
            logs=[
                "2024-01-01 ERROR: Database connection failed",
                "2024-01-01 INFO: Retrying...",
            ],
        )
        bi = BugInput(title="DB error", context=ctx)
        result = provider.to_normalized_review(
            bi, repo="acme/web", synthetic_pr_number=1
        )
        assert len(result["must_fix"]) >= 1


class TestGitHubProviders:
    def test_github_pr_provider(self):
        provider = GitHubPRBugProvider()
        bi = BugInput(
            provider=BugProviderKind.GITHUB_PR,
            title="PR review fix",
            source_url="https://github.com/acme/web/pull/42",
        )
        result = provider.to_normalized_review(
            bi, repo="acme/web", synthetic_pr_number=42
        )
        assert result["bug_provider"] == "github_pr"
        assert result["bug_source_url"] == "https://github.com/acme/web/pull/42"

    def test_github_issue_provider(self):
        provider = GitHubIssueBugProvider()
        bi = BugInput(
            provider=BugProviderKind.GITHUB_ISSUE,
            title="Issue fix",
            source_url="https://github.com/acme/web/issues/10",
            context=BugContext(error_messages=["Crash on startup"]),
        )
        result = provider.to_normalized_review(
            bi, repo="acme/web", synthetic_pr_number=1
        )
        assert result["bug_provider"] == "github_issue"
        assert "Crash on startup" in result["must_fix"][0]["text"]


class TestResolveProvider:
    def test_resolves_plaintext(self):
        bi = BugInput(title="test", provider=BugProviderKind.PLAINTEXT)
        provider = resolve_provider(bi)
        assert isinstance(provider, PlaintextBugProvider)

    def test_resolves_structured(self):
        bi = BugInput(title="test", provider=BugProviderKind.STRUCTURED)
        provider = resolve_provider(bi)
        assert isinstance(provider, StructuredBugProvider)

    def test_falls_back_to_plaintext(self, monkeypatch):
        from app.schemas.bug_input import BugProviderKind

        monkeypatch.setattr("app.services.bug_input.BUG_INPUT_PROVIDERS", [])
        provider = resolve_provider(
            BugInput(title="test", provider=BugProviderKind.PLAINTEXT)
        )
        assert isinstance(provider, PlaintextBugProvider)


class TestRegisterProvider:
    def test_register_and_replace(self):
        class CustomProvider:
            provider_kind = "custom_test"

            def supports(self, bug_input):
                return bug_input.provider.value == "custom_test"

            def to_normalized_review(self, bug_input, *, repo, synthetic_pr_number):
                return {}

        register_provider(CustomProvider())
        kinds = [p.provider_kind for p in BUG_INPUT_PROVIDERS]
        assert "custom_test" in kinds
        BUG_INPUT_PROVIDERS[:] = [
            p for p in BUG_INPUT_PROVIDERS if p.provider_kind != "custom_test"
        ]


class TestIdempotencyKey:
    def test_deterministic_key(self):
        key1 = build_bug_idempotency_key(
            repo="a/b", title="test", source_url="https://x.com"
        )
        key2 = build_bug_idempotency_key(
            repo="a/b", title="test", source_url="https://x.com"
        )
        assert key1 == key2

    def test_different_inputs_different_keys(self):
        key1 = build_bug_idempotency_key(repo="a/b", title="test1", source_url=None)
        key2 = build_bug_idempotency_key(repo="a/b", title="test2", source_url=None)
        assert key1 != key2


class TestSyntheticPRNumber:
    def test_deterministic(self):
        n1 = _synthetic_pr_number("a/b", "title")
        n2 = _synthetic_pr_number("a/b", "title")
        assert n1 == n2

    def test_in_offset_range(self):
        n = _synthetic_pr_number("a/b", "title")
        assert 9_000_001 <= n <= 9_999_999

    def test_different_title_different_number(self):
        n1 = _synthetic_pr_number("a/b", "title1")
        n2 = _synthetic_pr_number("a/b", "title2")
        assert n1 != n2


class TestExtractionHelpers:
    def test_extract_file_references(self):
        text = "Error in src/auth.py:42 and src/login.py"
        files = _extract_file_references(text)
        assert "src/auth.py" in files
        assert "src/login.py" in files

    def test_extract_error_messages(self):
        text = "INFO: starting\nERROR: connection failed\nERROR: timeout"
        errors = _extract_error_messages(text)
        assert len(errors) == 2
        assert any("connection failed" in e for e in errors)

    def test_extract_stacktraces_python(self):
        text = (
            "Traceback (most recent call last):\n  File 'a.py', line 1\nValueError: bad"
        )
        traces = _extract_stacktraces(text)
        assert len(traces) == 1
        assert "Traceback" in traces[0]


class TestBugSubmissionAPI:
    def test_submit_plaintext_bug(self, tmp_path):
        db_path = _setup_db(tmp_path)
        payload = {
            "provider": "plaintext",
            "title": "Fix login crash on mobile",
            "description": "App crashes when user taps login button",
            "repo": "acme/mobile-app",
        }
        with TestClient(app) as client:
            response = client.post("/api/bugs", json=payload)
        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is True
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
        assert str(row["trigger_source"]) == "bug_input"
        normalized = json.loads(str(row["normalized_review_json"]))
        assert normalized["source_kind"] == "bug_input"
        assert normalized["bug_provider"] == "plaintext"
        assert normalized["repo"] == "acme/mobile-app"
        assert "login crash" in normalized["must_fix"][0]["text"].lower()

    def test_submit_structured_bug(self, tmp_path):
        db_path = _setup_db(tmp_path)
        payload = {
            "provider": "structured",
            "title": "Auth module errors",
            "description": "Multiple auth failures",
            "repo": "acme/api",
            "context": {
                "files": ["src/auth.py", "src/session.py"],
                "error_messages": ["NullPointerError in auth", "Session timeout"],
                "stack_traces": ["Traceback: ..."],
            },
        }
        with TestClient(app) as client:
            response = client.post("/api/bugs", json=payload)
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
        normalized = json.loads(str(row["normalized_review_json"]))
        assert len(normalized["must_fix"]) >= 2

    def test_submit_log_stacktrace_bug(self, tmp_path):
        db_path = _setup_db(tmp_path)
        payload = {
            "provider": "log_stacktrace",
            "title": "Worker OOM",
            "description": "java.lang.OutOfMemoryError: Java heap space\n\tat com.acme.Worker.run(Worker.java:42)",
            "repo": "acme/service",
        }
        with TestClient(app) as client:
            response = client.post("/api/bugs", json=payload)
        assert response.status_code == 200
        data = response.json()
        assert data["queue_status"] == "queued"

    def test_submit_bug_dry_run(self, tmp_path):
        _setup_db(tmp_path)
        payload = {
            "title": "Dry run test",
            "description": "Should not create a run",
            "dry_run": True,
        }
        with TestClient(app) as client:
            response = client.post("/api/bugs", json=payload)
        assert response.status_code == 200
        data = response.json()
        assert data["queue_status"] == "validated"
        assert data["queued_run_id"] is None

    def test_submit_bug_without_repo(self, tmp_path):
        db_path = _setup_db(tmp_path)
        payload = {
            "title": "No repo bug",
            "description": "Just a description",
        }
        with TestClient(app) as client:
            response = client.post("/api/bugs", json=payload)
        assert response.status_code == 200
        data = response.json()
        assert data["queue_status"] == "queued"
        assert data["repo"] == "local/unspecified"

    def test_submit_bug_empty_title_rejected(self, tmp_path):
        _setup_db(tmp_path)
        payload = {"title": "", "description": "test"}
        with TestClient(app) as client:
            response = client.post("/api/bugs", json=payload)
        assert response.status_code == 422

    def test_list_providers(self, tmp_path):
        _setup_db(tmp_path)
        with TestClient(app) as client:
            response = client.get("/api/bugs/providers")
        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is True
        kinds = [p["kind"] for p in data["providers"]]
        assert "plaintext" in kinds
        assert "structured" in kinds
        assert "log_stacktrace" in kinds
        assert "github_pr" in kinds
        assert "github_issue" in kinds

    def test_idempotency_same_title_same_repo(self, tmp_path):
        db_path = _setup_db(tmp_path)
        payload = {
            "title": "Idempotent bug",
            "description": "Same bug submitted twice",
            "repo": "acme/test",
        }
        with TestClient(app) as client:
            r1 = client.post("/api/bugs", json=payload).json()
            r2 = client.post("/api/bugs", json=payload).json()
        assert r1["queue_status"] == "queued"
        assert r2["queue_status"] == "duplicate_task"
        assert r1["idempotency_key"] == r2["idempotency_key"]

    def test_github_issue_provider_via_api(self, tmp_path):
        db_path = _setup_db(tmp_path)
        payload = {
            "provider": "github_issue",
            "title": "GitHub issue bug",
            "description": "Issue from GitHub",
            "repo": "acme/web",
            "source_url": "https://github.com/acme/web/issues/99",
        }
        with TestClient(app) as client:
            response = client.post("/api/bugs", json=payload)
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
        normalized = json.loads(str(row["normalized_review_json"]))
        assert normalized["bug_provider"] == "github_issue"
