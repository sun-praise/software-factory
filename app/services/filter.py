from __future__ import annotations

import re
from collections.abc import Iterable
from functools import lru_cache

from app.config import get_settings
from app.services.runtime_settings import RuntimeSettings


@lru_cache(maxsize=64)
def _compile_pattern(pattern: str) -> re.Pattern | None:
    try:
        return re.compile(pattern, flags=re.IGNORECASE)
    except re.error:
        return None


def is_bot_actor(
    actor: str | None,
    *,
    bot_logins: Iterable[str] | None = None,
    runtime_settings: RuntimeSettings | None = None,
) -> bool:
    login = _normalize_value(actor)
    if login is None:
        return False
    if login.endswith("[bot]"):
        return True
    configured_logins = _normalize_values(
        _resolve_runtime_setting(
            explicit_value=bot_logins,
            runtime_value=(
                runtime_settings.bot_logins if runtime_settings is not None else None
            ),
            default_value=get_settings().bot_logins,
        )
    )
    return login in configured_logins


def is_noise_actor(
    actor: str | None,
    *,
    bot_logins: Iterable[str] | None = None,
    autofix_comment_author: str | None = None,
    runtime_settings: RuntimeSettings | None = None,
) -> bool:
    login = _normalize_value(actor)
    if login is None:
        return False

    author = _normalize_value(
        _resolve_runtime_setting(
            explicit_value=autofix_comment_author,
            runtime_value=(
                runtime_settings.autofix_comment_author
                if runtime_settings is not None
                else None
            ),
            default_value=get_settings().autofix_comment_author,
        )
    )
    if author and login == author:
        return True

    return is_bot_actor(
        login,
        bot_logins=bot_logins,
        runtime_settings=runtime_settings,
    )


def is_noise_comment(
    body: str | None,
    *,
    noise_comment_patterns: Iterable[str] | None = None,
    runtime_settings: RuntimeSettings | None = None,
) -> bool:
    text = body.strip() if isinstance(body, str) else ""
    if not text:
        return False

    patterns = tuple(
        _resolve_runtime_setting(
            explicit_value=noise_comment_patterns,
            runtime_value=(
                runtime_settings.noise_comment_patterns
                if runtime_settings is not None
                else None
            ),
            default_value=get_settings().noise_comment_patterns,
        )
    )
    for pattern in patterns:
        if _pattern_matches(text, pattern):
            return True
    return False


def is_managed_repo(
    repo: str | None,
    *,
    managed_repo_prefixes: Iterable[str] | None = None,
    runtime_settings: RuntimeSettings | None = None,
) -> bool:
    normalized_repo = _normalize_value(repo)
    if normalized_repo is None:
        return False

    prefixes = _normalize_values(
        _resolve_runtime_setting(
            explicit_value=managed_repo_prefixes,
            runtime_value=(
                runtime_settings.managed_repo_prefixes
                if runtime_settings is not None
                else None
            ),
            default_value=get_settings().managed_repo_prefixes,
        )
    )
    if not prefixes:
        return True

    return any(normalized_repo.startswith(prefix) for prefix in prefixes)


def get_filter_reason(
    repo: str | None,
    *,
    actor: str | None = None,
    body: str | None = None,
    bot_logins: Iterable[str] | None = None,
    noise_comment_patterns: Iterable[str] | None = None,
    managed_repo_prefixes: Iterable[str] | None = None,
    autofix_comment_author: str | None = None,
    runtime_settings: RuntimeSettings | None = None,
) -> str | None:
    if not is_managed_repo(
        repo,
        managed_repo_prefixes=managed_repo_prefixes,
        runtime_settings=runtime_settings,
    ):
        return "unmanaged_repo"
    if is_noise_actor(
        actor,
        bot_logins=bot_logins,
        autofix_comment_author=autofix_comment_author,
        runtime_settings=runtime_settings,
    ):
        return "noise_actor"
    if is_noise_comment(
        body,
        noise_comment_patterns=noise_comment_patterns,
        runtime_settings=runtime_settings,
    ):
        return "noise_comment"
    return None


def should_filter_event(
    repo: str | None,
    *,
    actor: str | None = None,
    body: str | None = None,
    bot_logins: Iterable[str] | None = None,
    noise_comment_patterns: Iterable[str] | None = None,
    managed_repo_prefixes: Iterable[str] | None = None,
    autofix_comment_author: str | None = None,
    runtime_settings: RuntimeSettings | None = None,
) -> bool:
    return (
        get_filter_reason(
            repo,
            actor=actor,
            body=body,
            bot_logins=bot_logins,
            noise_comment_patterns=noise_comment_patterns,
            managed_repo_prefixes=managed_repo_prefixes,
            autofix_comment_author=autofix_comment_author,
            runtime_settings=runtime_settings,
        )
        is not None
    )


def _normalize_values(values: Iterable[str]) -> tuple[str, ...]:
    normalized: list[str] = []
    for value in values:
        item = _normalize_value(value)
        if item is not None:
            normalized.append(item)
    return tuple(normalized)


def _resolve_runtime_setting(
    *,
    explicit_value,
    runtime_value,
    default_value,
):
    if explicit_value is not None:
        return explicit_value
    if runtime_value is not None:
        return runtime_value
    return default_value


def _normalize_value(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    return text or None


def _pattern_matches(text: str, pattern: str) -> bool:
    compiled = _compile_pattern(pattern)
    if compiled is None:
        escaped = re.compile(re.escape(pattern), flags=re.IGNORECASE)
        return escaped.search(text) is not None
    return compiled.search(text) is not None
