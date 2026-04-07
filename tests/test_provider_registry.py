from __future__ import annotations

from collections.abc import Callable
from typing import Any, Mapping

import pytest

from app.providers.registry import (
    FORGE_PROVIDER_CATEGORY,
    GIT_REMOTE_PROVIDER_CATEGORY,
    TASK_SOURCE_PROVIDER_CATEGORY,
    WEBHOOK_PROVIDER_CATEGORY,
    ProviderLookupError,
    ProviderRegistrationError,
    get_forge_provider,
    get_git_remote_provider,
    get_task_source_provider,
    get_webhook_provider,
    initialize_provider_registry,
    list_registered_provider_names,
    register_forge_provider,
    register_git_remote_provider,
    register_task_source_provider,
    register_webhook_provider,
    reset_provider_registry,
    resolve_provider_name,
    snapshot_registry,
)


@pytest.fixture(autouse=True)
def _reset_registry_state() -> None:
    reset_provider_registry(include_defaults=True)
    yield
    reset_provider_registry(include_defaults=True)


class _CustomForgeProvider:
    name = "custom"

    def ensure_pull_request(
        self,
        *,
        repo_dir: str,
        repo: str,
        head_branch: str,
        base_branch: str | None = None,
        title: str,
        body: str,
    ) -> Mapping[str, Any]:
        return {
            "success": True,
            "pr_number": 1,
            "pr_url": f"https://example.invalid/{repo}/pull/1",
            "error": None,
            "existing": False,
        }

    def post_pull_request_comment(
        self,
        *,
        repo_dir: str,
        repo: str,
        pr_number: int,
        body: str,
    ) -> tuple[bool, str]:
        return True, "ok"

    def get_pull_request_metadata(
        self,
        *,
        repo_dir: str,
        repo: str,
        pr_number: int,
    ) -> Mapping[str, Any]:
        return {"title": "demo"}

    def collect_changed_file_paths(
        self,
        *,
        repo_dir: str,
        repo: str,
        pr_number: int,
    ) -> list[str]:
        return ["app/main.py"]


class _CustomTaskSourceProvider:
    name = "custom"

    def parse_task_submission(
        self, *, submission: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        return submission

    def fetch_pull_request_feedback_review(
        self,
        *,
        repo: str,
        pr_number: int,
    ) -> Mapping[str, Any]:
        return {"repo": repo, "pr_number": pr_number}

    def resolve_pull_request_number_from_issue(
        self,
        *,
        repo: str,
        issue_number: int,
    ) -> int | None:
        return issue_number

    def resolve_manual_issue_context(
        self,
        *,
        repo: str,
        pr_number: int,
        issue_number: int | None,
        source_kind: str,
        source_ref: str,
        source_fragment: str,
        description_present: bool,
    ) -> Mapping[str, Any] | None:
        return {
            "text": "context",
            "path": None,
            "line": None,
            "source_url": source_ref,
        }


class _CustomWebhookProvider:
    name = "custom"

    @property
    def signature_header(self) -> str:
        return "X-Custom-Signature"

    def verify_signature(
        self,
        *,
        body: bytes,
        secret: str,
        signature_header: str | None,
    ) -> Mapping[str, Any]:
        return {
            "ok": True,
            "body_length": len(body),
            "secret": secret,
            "signature": signature_header,
        }

    def extract_review_event(
        self,
        *,
        event_type: str,
        payload: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        return {"type": event_type, "payload": payload}

    def extract_event_body(
        self,
        *,
        event_type: str,
        payload: Mapping[str, Any],
    ) -> str | None:
        return str(payload.get("body") or "") or None


class _CustomGitRemoteProvider:
    name = "custom"

    def build_clone_url(self, repo: str) -> str:
        return f"https://example.invalid/{repo}.git"

    def build_pull_request_url(self, *, repo: str, pr_number: int) -> str:
        return f"https://example.invalid/{repo}/pull/{pr_number}"

    @property
    def api_base_url(self) -> str:
        return "https://api.example.invalid"


def test_default_github_providers_are_registered() -> None:
    assert get_forge_provider().name == "github"
    assert get_task_source_provider().name == "github"
    assert get_webhook_provider().name == "github"
    assert get_git_remote_provider().name == "github"

    assert list_registered_provider_names(FORGE_PROVIDER_CATEGORY) == ("github",)
    assert list_registered_provider_names(TASK_SOURCE_PROVIDER_CATEGORY) == ("github",)
    assert list_registered_provider_names(WEBHOOK_PROVIDER_CATEGORY) == ("github",)
    assert list_registered_provider_names(GIT_REMOTE_PROVIDER_CATEGORY) == ("github",)


def test_resolve_provider_name_uses_default_and_normalizes_case() -> None:
    assert resolve_provider_name(None) == "github"
    assert resolve_provider_name("  GitHub  ") == "github"
    assert resolve_provider_name(" CUSTOM ") == "custom"


def test_register_forge_provider_supports_custom_lookup() -> None:
    custom = _CustomForgeProvider()
    register_forge_provider("custom", custom)

    assert get_forge_provider("custom") is custom
    assert list_registered_provider_names(FORGE_PROVIDER_CATEGORY) == (
        "custom",
        "github",
    )


def test_register_task_source_provider_supports_custom_lookup() -> None:
    custom = _CustomTaskSourceProvider()
    register_task_source_provider("custom", custom)

    assert get_task_source_provider("custom") is custom
    assert list_registered_provider_names(TASK_SOURCE_PROVIDER_CATEGORY) == (
        "custom",
        "github",
    )


def test_register_webhook_provider_supports_custom_lookup() -> None:
    custom = _CustomWebhookProvider()
    register_webhook_provider("custom", custom)

    assert get_webhook_provider("custom") is custom
    assert list_registered_provider_names(WEBHOOK_PROVIDER_CATEGORY) == (
        "custom",
        "github",
    )


def test_register_git_remote_provider_supports_custom_lookup() -> None:
    custom = _CustomGitRemoteProvider()
    register_git_remote_provider("custom", custom)

    assert get_git_remote_provider("custom") is custom
    assert list_registered_provider_names(GIT_REMOTE_PROVIDER_CATEGORY) == (
        "custom",
        "github",
    )


def test_register_provider_rejects_duplicate_normalized_name() -> None:
    register_forge_provider(" Custom ", _CustomForgeProvider())

    with pytest.raises(ProviderRegistrationError) as exc:
        register_forge_provider("custom", _CustomForgeProvider())

    assert "already registered" in str(exc.value)


def test_register_provider_can_replace_existing_provider() -> None:
    first = _CustomForgeProvider()
    second = _CustomForgeProvider()

    register_forge_provider("custom", first)
    register_forge_provider("custom", second, replace=True)

    assert get_forge_provider("custom") is second


def test_get_forge_provider_raises_for_unknown_provider_name() -> None:
    with pytest.raises(ProviderLookupError) as exc:
        get_forge_provider("missing")

    assert "available: github" in str(exc.value)


def test_list_registered_provider_names_raises_for_unknown_category() -> None:
    with pytest.raises(ProviderLookupError) as exc:
        list_registered_provider_names("unknown")

    assert "unknown provider category" in str(exc.value)


def test_reset_provider_registry_can_clear_all_defaults() -> None:
    reset_provider_registry(include_defaults=False)

    snapshot = snapshot_registry()
    assert snapshot.forge == ()
    assert snapshot.task_source == ()
    assert snapshot.webhook == ()
    assert snapshot.git_remote == ()

    with pytest.raises(ProviderLookupError):
        get_forge_provider()


def test_initialize_provider_registry_force_restores_defaults_after_clear() -> None:
    reset_provider_registry(include_defaults=False)
    initialize_provider_registry(force=True)

    assert get_forge_provider().name == "github"
    assert get_task_source_provider().name == "github"
    assert get_webhook_provider().name == "github"
    assert get_git_remote_provider().name == "github"


def test_initialize_provider_registry_is_idempotent_without_force() -> None:
    first = initialize_provider_registry()
    second = initialize_provider_registry()

    assert first == second


@pytest.mark.parametrize(
    ("register_fn", "provider_name"),
    [
        (register_forge_provider, "forge"),
        (register_task_source_provider, "task_source"),
        (register_webhook_provider, "webhook"),
        (register_git_remote_provider, "git_remote"),
    ],
)
def test_register_provider_rejects_invalid_protocol_implementations(
    register_fn: Callable[..., None],
    provider_name: str,
) -> None:
    with pytest.raises(ProviderRegistrationError) as exc:
        register_fn("invalid", object())  # type: ignore[arg-type]

    message = str(exc.value)
    assert "does not implement required protocol" in message
    assert provider_name in message
