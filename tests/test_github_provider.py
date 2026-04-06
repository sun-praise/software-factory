from __future__ import annotations

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
