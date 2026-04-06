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
