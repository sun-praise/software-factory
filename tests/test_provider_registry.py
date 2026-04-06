from __future__ import annotations

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
    list_registered_provider_names,
    register_forge_provider,
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
        base_branch: str,
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


def test_register_forge_provider_rejects_duplicate_without_replace() -> None:
    register_forge_provider("custom", _CustomForgeProvider())

    with pytest.raises(ProviderRegistrationError) as exc:
        register_forge_provider("custom", _CustomForgeProvider())

    assert "already registered" in str(exc.value)


def test_register_forge_provider_can_replace_existing_provider() -> None:
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
