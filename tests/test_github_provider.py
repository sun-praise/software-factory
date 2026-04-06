from __future__ import annotations

import json
import subprocess

import pytest

from app.providers import github as github_provider


def test_github_forge_provider_ensure_pull_request_delegates_to_git_ops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_ensure_pull_request(
        repo_dir: str,
        repo: str,
        head_branch: str,
        *,
        title: str,
        body: str,
        base_branch: str | None = None,
        remote: str = "origin",
    ) -> dict[str, object]:
        captured.update(
            {
                "repo_dir": repo_dir,
                "repo": repo,
                "head_branch": head_branch,
                "title": title,
                "body": body,
                "base_branch": base_branch,
                "remote": remote,
            }
        )
        return {
            "success": True,
            "pr_number": 12,
            "pr_url": "https://github.com/acme/widgets/pull/12",
            "error": None,
            "existing": False,
        }

    monkeypatch.setattr(
        github_provider, "ensure_pull_request", fake_ensure_pull_request
    )

    provider = github_provider.GitHubForgeProvider()
    result = provider.ensure_pull_request(
        repo_dir="/repo",
        repo="acme/widgets",
        head_branch="feature/m5",
        base_branch=None,
        title="Fix bug",
        body="details",
    )

    assert result["success"] is True
    assert captured == {
        "repo_dir": "/repo",
        "repo": "acme/widgets",
        "head_branch": "feature/m5",
        "title": "Fix bug",
        "body": "details",
        "base_branch": None,
        "remote": "origin",
    }


def test_github_forge_provider_collect_changed_file_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout="app/main.py\napp/utils.py\n",
            stderr="",
        )

    monkeypatch.setattr(github_provider.subprocess, "run", fake_run)

    provider = github_provider.GitHubForgeProvider()
    result = provider.collect_changed_file_paths(
        repo_dir="/repo",
        repo="acme/widgets",
        pr_number=7,
    )

    assert result == ["app/main.py", "app/utils.py"]


def test_github_forge_provider_collect_changed_file_paths_truncates_to_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = "\n".join(f"file_{i}.py" for i in range(60))

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout=paths,
            stderr="",
        )

    monkeypatch.setattr(github_provider.subprocess, "run", fake_run)

    provider = github_provider.GitHubForgeProvider()
    result = provider.collect_changed_file_paths(
        repo_dir="/repo",
        repo="acme/widgets",
        pr_number=7,
    )

    assert len(result) == github_provider.CHANGED_FILE_PATHS_LIMIT
    assert result[0] == "file_0.py"


def test_github_forge_provider_get_pull_request_metadata_returns_none_on_gh_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(*args, **kwargs):
        raise FileNotFoundError("gh")

    monkeypatch.setattr(github_provider.subprocess, "run", fake_run)

    provider = github_provider.GitHubForgeProvider()
    metadata = provider.get_pull_request_metadata(
        repo_dir="/repo",
        repo="acme/widgets",
        pr_number=7,
    )

    assert metadata is None


def test_github_forge_provider_metadata_retries_without_can_be_rebased(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_run(*args, **kwargs):
        command = list(args[0])
        cmd_str = " ".join(command)
        calls.append(command)
        if "diff" in command:
            return subprocess.CompletedProcess(
                args=command,
                returncode=0,
                stdout="file1.py\n",
                stderr="",
            )
        if "view" in command and "canBeRebased" in cmd_str:
            return subprocess.CompletedProcess(
                args=command,
                returncode=1,
                stdout="",
                stderr='Unknown JSON field: "canBeRebased"',
            )
        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout=json.dumps(
                {
                    "title": "Fallback PR",
                    "baseRefName": "main",
                    "headRefName": "feature",
                    "headRefOid": "abc123",
                    "changedFiles": 2,
                    "additions": 4,
                    "deletions": 1,
                    "mergeStateStatus": "BEHIND",
                    "mergeable": True,
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(github_provider.subprocess, "run", fake_run)

    provider = github_provider.GitHubForgeProvider()
    metadata = provider.get_pull_request_metadata(
        repo_dir="/repo",
        repo="acme/widgets",
        pr_number=7,
    )

    assert metadata is not None
    assert metadata["title"] == "Fallback PR"
    assert metadata["merge_state_status"] == "BEHIND"
    assert metadata["is_behind"] is True
    assert metadata["can_be_rebased"] is None
    assert metadata["changed_file_paths"] == ["file1.py"]
    view_calls = [c for c in calls if "view" in c]
    assert len(view_calls) == 2
    assert "canBeRebased" in " ".join(view_calls[0])
    assert "canBeRebased" not in " ".join(view_calls[1])


def test_github_forge_provider_metadata_returns_none_when_fallback_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"count": 0}

    def fake_run(*args, **kwargs):
        command = list(args[0])
        if "view" not in command:
            return subprocess.CompletedProcess(
                args=command,
                returncode=0,
                stdout="",
                stderr="",
            )
        calls["count"] += 1
        if calls["count"] == 1:
            return subprocess.CompletedProcess(
                args=command,
                returncode=1,
                stdout="",
                stderr='Unknown JSON field: "canBeRebased"',
            )
        return subprocess.CompletedProcess(
            args=command,
            returncode=1,
            stdout="",
            stderr="GraphQL: Could not resolve to a PullRequest",
        )

    monkeypatch.setattr(github_provider.subprocess, "run", fake_run)

    provider = github_provider.GitHubForgeProvider()
    metadata = provider.get_pull_request_metadata(
        repo_dir="/repo",
        repo="acme/widgets",
        pr_number=7,
    )

    assert metadata is None
    assert calls["count"] == 2


def test_github_forge_provider_metadata_returns_none_on_invalid_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(*args, **kwargs):
        command = list(args[0])
        if "diff" in command:
            return subprocess.CompletedProcess(
                args=command,
                returncode=0,
                stdout="",
                stderr="",
            )
        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout="not valid json{{{",
            stderr="",
        )

    monkeypatch.setattr(github_provider.subprocess, "run", fake_run)

    provider = github_provider.GitHubForgeProvider()
    metadata = provider.get_pull_request_metadata(
        repo_dir="/repo",
        repo="acme/widgets",
        pr_number=7,
    )

    assert metadata is None


def test_github_forge_provider_metadata_returns_none_on_non_mapping_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(*args, **kwargs):
        command = list(args[0])
        if "diff" in command:
            return subprocess.CompletedProcess(
                args=command,
                returncode=0,
                stdout="",
                stderr="",
            )
        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout='["not", "a", "mapping"]',
            stderr="",
        )

    monkeypatch.setattr(github_provider.subprocess, "run", fake_run)

    provider = github_provider.GitHubForgeProvider()
    metadata = provider.get_pull_request_metadata(
        repo_dir="/repo",
        repo="acme/widgets",
        pr_number=7,
    )

    assert metadata is None


def test_github_task_source_provider_parses_text_submission() -> None:
    provider = github_provider.GitHubTaskSourceProvider()

    parsed = provider.parse_task_submission(
        submission={
            "repo": "acme/widgets",
            "title": "Fix startup crash",
            "text": "The app crashes on startup.",
        }
    )

    assert parsed["repo"] == "acme/widgets"
    assert parsed["owner"] == "acme"
    assert parsed["repo_name"] == "widgets"
    assert parsed["source_kind"] == "text"
    assert parsed["task_title"] == "Fix startup crash"
    assert parsed["task_text"] == "The app crashes on startup."
    assert isinstance(parsed["pr_number"], int)
    assert parsed["pr_number"] > 0


def test_github_task_source_provider_parses_pull_url_without_api_call() -> None:
    provider = github_provider.GitHubTaskSourceProvider()

    parsed = provider.parse_task_submission(
        submission={"url": "https://github.com/acme/widgets/pull/42"}
    )

    assert parsed == {
        "repo": "acme/widgets",
        "owner": "acme",
        "repo_name": "widgets",
        "pr_number": 42,
        "resolved_pr_number": 42,
        "issue_number": None,
        "source_ref": "https://github.com/acme/widgets/pull/42",
        "source_fragment": "",
        "source_kind": "pull",
        "task_title": None,
        "task_text": None,
    }


def test_github_task_source_provider_parses_issue_url_with_pr_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = github_provider.GitHubTaskSourceProvider()
    monkeypatch.setattr(
        provider,
        "resolve_pull_request_number_from_issue",
        lambda *, repo, issue_number: 88,
    )

    parsed = provider.parse_task_submission(
        submission={"url": "https://github.com/acme/widgets/issues/99"}
    )

    assert parsed["repo"] == "acme/widgets"
    assert parsed["pr_number"] == 88
    assert parsed["resolved_pr_number"] == 88
    assert parsed["issue_number"] == 99
    assert parsed["source_kind"] == "issue"


def test_github_webhook_provider_delegates_to_service_helpers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, object] = {}

    def fake_verify_github_signature(*, body, secret, signature_header):
        calls["verify"] = {
            "body": body,
            "secret": secret,
            "signature_header": signature_header,
        }
        return {"status": "verified"}

    def fake_extract_review_event(*, event_type, payload):
        calls["event"] = {
            "event_type": event_type,
            "payload": payload,
        }
        return {"repo": "acme/widgets", "pr_number": 42}

    def fake_extract_event_body(event_type, payload):
        calls["body"] = {
            "event_type": event_type,
            "payload": payload,
        }
        return "Please fix"

    monkeypatch.setattr(
        github_provider,
        "verify_github_signature",
        fake_verify_github_signature,
    )
    monkeypatch.setattr(
        github_provider,
        "extract_review_event",
        fake_extract_review_event,
    )
    monkeypatch.setattr(
        github_provider,
        "extract_event_body",
        fake_extract_event_body,
    )

    provider = github_provider.GitHubWebhookProvider()
    verify_result = provider.verify_signature(
        body=b"{}",
        secret="top-secret",
        signature_header="sha256=abc",
    )
    event_result = provider.extract_review_event(
        event_type="pull_request_review",
        payload={"review": {"id": 1}},
    )
    body_result = provider.extract_event_body(
        event_type="pull_request_review",
        payload={"review": {"body": "Please fix"}},
    )

    assert provider.signature_header == "X-Hub-Signature-256"
    assert verify_result == {"status": "verified"}
    assert event_result == {"repo": "acme/widgets", "pr_number": 42}
    assert body_result == "Please fix"
    assert calls["verify"] == {
        "body": b"{}",
        "secret": "top-secret",
        "signature_header": "sha256=abc",
    }
    assert calls["event"] == {
        "event_type": "pull_request_review",
        "payload": {"review": {"id": 1}},
    }
    assert calls["body"] == {
        "event_type": "pull_request_review",
        "payload": {"review": {"body": "Please fix"}},
    }


def test_github_git_remote_provider_builds_expected_urls() -> None:
    provider = github_provider.GitHubGitRemoteProvider()

    assert (
        provider.build_clone_url("acme/widgets")
        == "https://github.com/acme/widgets.git"
    )
    assert (
        provider.build_pull_request_url(repo="acme/widgets", pr_number=42)
        == "https://github.com/acme/widgets/pull/42"
    )
    assert provider.api_base_url == "https://api.github.com"
