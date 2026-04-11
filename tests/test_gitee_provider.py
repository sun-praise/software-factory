from __future__ import annotations

import json
from urllib.parse import quote_plus

import pytest

from app.providers import gitee as gitee_provider
from app.services.github_events import GitHubReviewEvent


def _build_gitee_signature(*, secret: str, timestamp: str) -> str:
    return quote_plus(
        gitee_provider._build_gitee_signature(secret=secret, timestamp=timestamp)
    )


def test_gitee_git_remote_provider_builds_expected_urls() -> None:
    provider = gitee_provider.GiteeGitRemoteProvider()

    assert (
        provider.build_clone_url("acme/widgets") == "https://gitee.com/acme/widgets.git"
    )
    assert (
        provider.build_pull_request_url(repo="acme/widgets", pr_number=42)
        == "https://gitee.com/acme/widgets/pulls/42"
    )
    assert provider.api_base_url == "https://gitee.com/api/v5"


def test_gitee_task_source_provider_parses_pull_url() -> None:
    provider = gitee_provider.GiteeTaskSourceProvider()

    parsed = provider.parse_task_submission(
        submission={"url": "https://gitee.com/acme/widgets/pulls/42"}
    )

    assert parsed == {
        "repo": "acme/widgets",
        "owner": "acme",
        "repo_name": "widgets",
        "pr_number": 42,
        "resolved_pr_number": 42,
        "issue_number": None,
        "source_ref": "https://gitee.com/acme/widgets/pulls/42",
        "source_fragment": "",
        "source_kind": "pull",
        "task_title": None,
        "task_text": None,
    }


def test_gitee_webhook_provider_accepts_password_mode_signature() -> None:
    provider = gitee_provider.GiteeWebhookProvider()

    result = provider.verify_signature(
        body=b"{}",
        secret="top-secret",
        signature_header="top-secret",
        request_headers={"x-gitee-event": "Note Hook"},
    )

    assert result.ok is True


def test_gitee_webhook_provider_accepts_signed_token_with_timestamp() -> None:
    provider = gitee_provider.GiteeWebhookProvider()
    timestamp = "1710000000000"

    result = provider.verify_signature(
        body=b"{}",
        secret="SEC123",
        signature_header=_build_gitee_signature(secret="SEC123", timestamp=timestamp),
        request_headers={
            "x-gitee-event": "Note Hook",
            "x-gitee-timestamp": timestamp,
        },
    )

    assert result.ok is True


def test_gitee_webhook_provider_extracts_pull_request_comment_event() -> None:
    provider = gitee_provider.GiteeWebhookProvider()

    event = provider.extract_review_event(
        event_type="Note Hook",
        payload={
            "repository": {"path_with_namespace": "acme/widgets"},
            "noteable_type": "PullRequest",
            "comment": {
                "id": 3001,
                "body": "Please fix",
                "user": {"login": "reviewer"},
            },
            "pull_request": {
                "number": 42,
                "head": {"sha": "abc123"},
            },
            "sender": {"login": "reviewer"},
        },
    )

    assert isinstance(event, GitHubReviewEvent)
    assert event.repo == "acme/widgets"
    assert event.pr_number == 42
    assert event.event_type == "issue_comment"
    assert event.actor == "reviewer"
    assert event.head_sha == "abc123"


def test_gitee_webhook_provider_enrich_event_fetches_pr_info(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class _Response:
        status_code = 200

        def json(self):
            return {
                "head": {"sha": "abc123", "ref": "feature/test"},
                "number": 42,
            }

    def fake_get(url: str, *, headers, timeout: float):
        captured["url"] = url
        captured["headers"] = dict(headers)
        captured["timeout"] = timeout
        return _Response()

    monkeypatch.setattr(gitee_provider.httpx, "get", fake_get)

    provider = gitee_provider.GiteeWebhookProvider()
    event = GitHubReviewEvent(
        repo="acme/widgets",
        pr_number=42,
        event_type="issue_comment",
        event_id="3001",
        event_key="gitee:issue_comment:acme/widgets:42:3001",
        actor="reviewer",
        head_sha=None,
        raw_payload_json="{}",
    )
    payload = {
        "repository": {"path_with_namespace": "acme/widgets"},
        "comment": {"id": 3001, "body": "Please fix"},
        "pull_request": {"number": 42},
    }

    enriched_event, enriched_payload = provider.enrich_event_pull_request_info(
        event=event,
        payload=payload,
        github_token="gitee-token",
    )

    assert enriched_event.head_sha == "abc123"
    assert enriched_payload["pull_request"]["head"]["sha"] == "abc123"
    assert captured["url"] == "https://gitee.com/api/v5/repos/acme/widgets/pulls/42"
    assert captured["headers"] == {
        "Authorization": "token gitee-token",
        "Accept": "application/json",
        "User-Agent": "software-factory",
    }


def test_gitee_forge_provider_collect_changed_file_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class _Response:
        status_code = 200

        def json(self):
            return [
                {"filename": "app/main.py"},
                {"filename": "app/routes/web.py"},
            ]

    def fake_get(url: str, *, headers, timeout: float):
        captured["url"] = url
        captured["headers"] = dict(headers)
        captured["timeout"] = timeout
        return _Response()

    monkeypatch.setattr(gitee_provider.httpx, "get", fake_get)
    monkeypatch.setenv("GITEE_TOKEN", "gitee-token")

    provider = gitee_provider.GiteeForgeProvider()
    result = provider.collect_changed_file_paths(
        repo_dir="/repo",
        repo="acme/widgets",
        pr_number=7,
    )

    assert result == ["app/main.py", "app/routes/web.py"]
    assert (
        captured["url"] == "https://gitee.com/api/v5/repos/acme/widgets/pulls/7/files"
    )
