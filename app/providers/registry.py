from __future__ import annotations

from dataclasses import dataclass
import threading
from typing import Any, Mapping, cast

from app.providers.types import (
    DEFAULT_PROVIDER_NAME,
    ForgeProvider,
    GitRemoteProvider,
    TaskSourceProvider,
    WebhookProvider,
)


FORGE_PROVIDER_CATEGORY = "forge"
TASK_SOURCE_PROVIDER_CATEGORY = "task_source"
WEBHOOK_PROVIDER_CATEGORY = "webhook"
GIT_REMOTE_PROVIDER_CATEGORY = "git_remote"
_PROVIDER_CATEGORIES = frozenset(
    {
        FORGE_PROVIDER_CATEGORY,
        TASK_SOURCE_PROVIDER_CATEGORY,
        WEBHOOK_PROVIDER_CATEGORY,
        GIT_REMOTE_PROVIDER_CATEGORY,
    }
)


class ProviderRegistryError(RuntimeError):
    """Base error for provider registration and lookups."""


class ProviderRegistrationError(ProviderRegistryError):
    """Raised when provider registration is invalid."""


class ProviderLookupError(ProviderRegistryError):
    """Raised when a provider lookup fails."""


@dataclass(frozen=True, slots=True)
class RegistrySnapshot:
    forge: tuple[str, ...]
    task_source: tuple[str, ...]
    webhook: tuple[str, ...]
    git_remote: tuple[str, ...]


_forge_providers: dict[str, ForgeProvider] = {}
_task_source_providers: dict[str, TaskSourceProvider] = {}
_webhook_providers: dict[str, WebhookProvider] = {}
_git_remote_providers: dict[str, GitRemoteProvider] = {}
_REGISTRY_LOCK = threading.RLock()
_registry_initialized = False


def resolve_provider_name(
    name: str | None, *, default_name: str = DEFAULT_PROVIDER_NAME
) -> str:
    candidate = _normalize_provider_name(name)
    if candidate is not None:
        return candidate
    fallback = _normalize_provider_name(default_name)
    if fallback is None:
        raise ProviderLookupError("default provider name cannot be empty")
    return fallback


def initialize_provider_registry(*, force: bool = False) -> RegistrySnapshot:
    """Initialize defaults once, or force-reset to defaults."""
    global _registry_initialized
    with _REGISTRY_LOCK:
        if force:
            _clear_registry_locked()
            _register_builtin_defaults_locked()
            _registry_initialized = True
        else:
            _ensure_initialized_locked()
        return _snapshot_registry_locked()


def register_forge_provider(
    name: str,
    provider: ForgeProvider,
    *,
    replace: bool = False,
) -> None:
    normalized_name = _normalize_provider_name(name)
    if normalized_name is None:
        raise ProviderRegistrationError("provider name cannot be empty")
    _validate_provider(
        category=FORGE_PROVIDER_CATEGORY,
        name=normalized_name,
        provider=provider,
        expected_protocol=ForgeProvider,
    )
    with _REGISTRY_LOCK:
        _ensure_initialized_locked()
        _register_provider_locked(
            store=_forge_providers,
            category=FORGE_PROVIDER_CATEGORY,
            name=normalized_name,
            provider=provider,
            replace=replace,
        )


def register_task_source_provider(
    name: str,
    provider: TaskSourceProvider,
    *,
    replace: bool = False,
) -> None:
    normalized_name = _normalize_provider_name(name)
    if normalized_name is None:
        raise ProviderRegistrationError("provider name cannot be empty")
    _validate_provider(
        category=TASK_SOURCE_PROVIDER_CATEGORY,
        name=normalized_name,
        provider=provider,
        expected_protocol=TaskSourceProvider,
    )
    with _REGISTRY_LOCK:
        _ensure_initialized_locked()
        _register_provider_locked(
            store=_task_source_providers,
            category=TASK_SOURCE_PROVIDER_CATEGORY,
            name=normalized_name,
            provider=provider,
            replace=replace,
        )


def register_webhook_provider(
    name: str,
    provider: WebhookProvider,
    *,
    replace: bool = False,
) -> None:
    normalized_name = _normalize_provider_name(name)
    if normalized_name is None:
        raise ProviderRegistrationError("provider name cannot be empty")
    _validate_provider(
        category=WEBHOOK_PROVIDER_CATEGORY,
        name=normalized_name,
        provider=provider,
        expected_protocol=WebhookProvider,
    )
    with _REGISTRY_LOCK:
        _ensure_initialized_locked()
        _register_provider_locked(
            store=_webhook_providers,
            category=WEBHOOK_PROVIDER_CATEGORY,
            name=normalized_name,
            provider=provider,
            replace=replace,
        )


def register_git_remote_provider(
    name: str,
    provider: GitRemoteProvider,
    *,
    replace: bool = False,
) -> None:
    normalized_name = _normalize_provider_name(name)
    if normalized_name is None:
        raise ProviderRegistrationError("provider name cannot be empty")
    _validate_provider(
        category=GIT_REMOTE_PROVIDER_CATEGORY,
        name=normalized_name,
        provider=provider,
        expected_protocol=GitRemoteProvider,
    )
    with _REGISTRY_LOCK:
        _ensure_initialized_locked()
        _register_provider_locked(
            store=_git_remote_providers,
            category=GIT_REMOTE_PROVIDER_CATEGORY,
            name=normalized_name,
            provider=provider,
            replace=replace,
        )


def get_forge_provider(name: str | None = None) -> ForgeProvider:
    resolved_name = resolve_provider_name(name)
    with _REGISTRY_LOCK:
        _ensure_initialized_locked()
        return cast(
            ForgeProvider,
            _get_provider_locked(
                store=_forge_providers,
                category=FORGE_PROVIDER_CATEGORY,
                name=resolved_name,
            ),
        )


def get_task_source_provider(name: str | None = None) -> TaskSourceProvider:
    resolved_name = resolve_provider_name(name)
    with _REGISTRY_LOCK:
        _ensure_initialized_locked()
        return cast(
            TaskSourceProvider,
            _get_provider_locked(
                store=_task_source_providers,
                category=TASK_SOURCE_PROVIDER_CATEGORY,
                name=resolved_name,
            ),
        )


def get_webhook_provider(name: str | None = None) -> WebhookProvider:
    resolved_name = resolve_provider_name(name)
    with _REGISTRY_LOCK:
        _ensure_initialized_locked()
        return cast(
            WebhookProvider,
            _get_provider_locked(
                store=_webhook_providers,
                category=WEBHOOK_PROVIDER_CATEGORY,
                name=resolved_name,
            ),
        )


def get_git_remote_provider(name: str | None = None) -> GitRemoteProvider:
    resolved_name = resolve_provider_name(name)
    with _REGISTRY_LOCK:
        _ensure_initialized_locked()
        return cast(
            GitRemoteProvider,
            _get_provider_locked(
                store=_git_remote_providers,
                category=GIT_REMOTE_PROVIDER_CATEGORY,
                name=resolved_name,
            ),
        )


def list_registered_provider_names(category: str) -> tuple[str, ...]:
    normalized_category = category.strip().lower()
    if normalized_category not in _PROVIDER_CATEGORIES:
        raise ProviderLookupError(
            f"unknown provider category '{category}'. "
            f"expected one of: {', '.join(sorted(_PROVIDER_CATEGORIES))}"
        )

    with _REGISTRY_LOCK:
        _ensure_initialized_locked()
        if normalized_category == FORGE_PROVIDER_CATEGORY:
            return tuple(sorted(_forge_providers))
        if normalized_category == TASK_SOURCE_PROVIDER_CATEGORY:
            return tuple(sorted(_task_source_providers))
        if normalized_category == WEBHOOK_PROVIDER_CATEGORY:
            return tuple(sorted(_webhook_providers))
        return tuple(sorted(_git_remote_providers))


def snapshot_registry() -> RegistrySnapshot:
    with _REGISTRY_LOCK:
        _ensure_initialized_locked()
        return _snapshot_registry_locked()


def reset_provider_registry(*, include_defaults: bool = True) -> None:
    global _registry_initialized
    with _REGISTRY_LOCK:
        _clear_registry_locked()
        if include_defaults:
            _register_builtin_defaults_locked()
        # Mark initialized even when defaults are excluded so callers can
        # intentionally keep the registry empty without lazy re-initialization.
        _registry_initialized = True


def _validate_provider(
    *,
    category: str,
    name: str,
    provider: Any,
    expected_protocol: type[Any],
) -> None:
    if provider is None:
        raise ProviderRegistrationError(f"{category} provider '{name}' cannot be None")
    if not isinstance(provider, expected_protocol):
        protocol_name = getattr(expected_protocol, "__name__", str(expected_protocol))
        raise ProviderRegistrationError(
            f"{category} provider '{name}' does not implement required protocol {protocol_name}"
        )


def _register_provider_locked(
    *,
    store: dict[str, Any],
    category: str,
    name: str,
    provider: Any,
    replace: bool,
) -> None:
    if not replace and name in store:
        raise ProviderRegistrationError(
            f"{category} provider '{name}' is already registered"
        )
    store[name] = provider


def _get_provider_locked(*, store: Mapping[str, Any], category: str, name: str) -> Any:
    normalized_name = _normalize_provider_name(name)
    if normalized_name is None:
        raise ProviderLookupError(f"{category} provider name cannot be empty")
    provider = store.get(normalized_name)
    if provider is None:
        available = tuple(sorted(store))
        if available:
            raise ProviderLookupError(
                f"{category} provider '{normalized_name}' is not registered. "
                f"available: {', '.join(available)}"
            )
        raise ProviderLookupError(
            f"{category} provider '{normalized_name}' is not registered"
        )
    return provider


def _snapshot_registry_locked() -> RegistrySnapshot:
    return RegistrySnapshot(
        forge=tuple(sorted(_forge_providers)),
        task_source=tuple(sorted(_task_source_providers)),
        webhook=tuple(sorted(_webhook_providers)),
        git_remote=tuple(sorted(_git_remote_providers)),
    )


def _ensure_initialized_locked() -> None:
    global _registry_initialized
    if _registry_initialized:
        return
    _register_builtin_defaults_locked()
    _registry_initialized = True


def _clear_registry_locked() -> None:
    _forge_providers.clear()
    _task_source_providers.clear()
    _webhook_providers.clear()
    _git_remote_providers.clear()


def _normalize_provider_name(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip().lower()
    return normalized or None


class _GitHubForgeProviderStub:
    name = DEFAULT_PROVIDER_NAME

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
        raise NotImplementedError(
            "GitHub forge provider is not wired yet; phase 2 will attach git_ops-backed implementation"
        )

    def post_pull_request_comment(
        self,
        *,
        repo_dir: str,
        repo: str,
        pr_number: int,
        body: str,
    ) -> tuple[bool, str]:
        raise NotImplementedError(
            "GitHub forge provider is not wired yet; phase 2 will attach git_ops-backed implementation"
        )

    def get_pull_request_metadata(
        self,
        *,
        repo_dir: str,
        repo: str,
        pr_number: int,
    ) -> Mapping[str, Any] | None:
        raise NotImplementedError(
            "GitHub forge provider is not wired yet; phase 2 will attach gh-backed metadata implementation"
        )

    def collect_changed_file_paths(
        self,
        *,
        repo_dir: str,
        repo: str,
        pr_number: int,
    ) -> list[str]:
        raise NotImplementedError(
            "GitHub forge provider is not wired yet; phase 2 will attach gh-backed diff implementation"
        )


class _GitHubTaskSourceProviderStub:
    name = DEFAULT_PROVIDER_NAME

    def parse_task_submission(
        self, *, submission: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        raise NotImplementedError(
            "GitHub task-source provider is not wired yet; phase 3 will attach web route parsing"
        )

    def fetch_pull_request_feedback_review(
        self,
        *,
        repo: str,
        pr_number: int,
    ) -> Mapping[str, Any]:
        raise NotImplementedError(
            "GitHub task-source provider is not wired yet; phase 3 will attach API fetch logic"
        )

    def resolve_pull_request_number_from_issue(
        self,
        *,
        repo: str,
        issue_number: int,
    ) -> int | None:
        raise NotImplementedError(
            "GitHub task-source provider is not wired yet; phase 3 will attach issue->PR resolution"
        )


class _GitHubWebhookProviderStub:
    name = DEFAULT_PROVIDER_NAME

    @property
    def signature_header(self) -> str:
        return "X-Hub-Signature-256"

    def verify_signature(
        self,
        *,
        body: bytes,
        secret: str,
        signature_header: str | None,
    ) -> Any:
        raise NotImplementedError(
            "GitHub webhook provider is not wired yet; phase 4 will attach signature verification"
        )

    def extract_review_event(
        self,
        *,
        event_type: str,
        payload: Mapping[str, Any],
    ) -> Any:
        raise NotImplementedError(
            "GitHub webhook provider is not wired yet; phase 4 will attach event extraction"
        )

    def extract_event_body(
        self,
        *,
        event_type: str,
        payload: Mapping[str, Any],
    ) -> str | None:
        raise NotImplementedError(
            "GitHub webhook provider is not wired yet; phase 4 will attach event-body extraction"
        )


class _GitHubGitRemoteProvider:
    name = DEFAULT_PROVIDER_NAME

    def build_clone_url(self, repo: str) -> str:
        return f"https://github.com/{repo}.git"

    def build_pull_request_url(self, *, repo: str, pr_number: int) -> str:
        return f"https://github.com/{repo}/pull/{pr_number}"

    @property
    def api_base_url(self) -> str:
        return "https://api.github.com"


def _register_builtin_defaults_locked() -> None:
    _register_provider_locked(
        store=_forge_providers,
        category=FORGE_PROVIDER_CATEGORY,
        name=DEFAULT_PROVIDER_NAME,
        provider=_GitHubForgeProviderStub(),
        replace=False,
    )
    _register_provider_locked(
        store=_task_source_providers,
        category=TASK_SOURCE_PROVIDER_CATEGORY,
        name=DEFAULT_PROVIDER_NAME,
        provider=_GitHubTaskSourceProviderStub(),
        replace=False,
    )
    _register_provider_locked(
        store=_webhook_providers,
        category=WEBHOOK_PROVIDER_CATEGORY,
        name=DEFAULT_PROVIDER_NAME,
        provider=_GitHubWebhookProviderStub(),
        replace=False,
    )
    _register_provider_locked(
        store=_git_remote_providers,
        category=GIT_REMOTE_PROVIDER_CATEGORY,
        name=DEFAULT_PROVIDER_NAME,
        provider=_GitHubGitRemoteProvider(),
        replace=False,
    )
