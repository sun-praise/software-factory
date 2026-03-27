from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from typing import Any, Mapping

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


RUNTIME_GITHUB_WEBHOOK_DEBOUNCE_SECONDS_KEY = "runtime.github_webhook_debounce_seconds"
RUNTIME_MAX_AUTOFIX_PER_PR_KEY = "runtime.max_autofix_per_pr"
RUNTIME_MAX_CONCURRENT_RUNS_KEY = "runtime.max_concurrent_runs"
RUNTIME_STALE_RUN_TIMEOUT_SECONDS_KEY = "runtime.stale_run_timeout_seconds"
RUNTIME_PR_LOCK_TTL_SECONDS_KEY = "runtime.pr_lock_ttl_seconds"
RUNTIME_MAX_RETRY_ATTEMPTS_KEY = "runtime.max_retry_attempts"
RUNTIME_RETRY_BACKOFF_BASE_SECONDS_KEY = "runtime.retry_backoff_base_seconds"
RUNTIME_RETRY_BACKOFF_MAX_SECONDS_KEY = "runtime.retry_backoff_max_seconds"
RUNTIME_BOT_LOGINS_KEY = "runtime.bot_logins"
RUNTIME_NOISE_COMMENT_PATTERNS_KEY = "runtime.noise_comment_patterns"
RUNTIME_MANAGED_REPO_PREFIXES_KEY = "runtime.managed_repo_prefixes"
RUNTIME_AUTOFIX_COMMENT_AUTHOR_KEY = "runtime.autofix_comment_author"


_LOG = logging.getLogger(__name__)
_LIST_OVERRIDE_FIELDS = (
    "bot_logins",
    "noise_comment_patterns",
    "managed_repo_prefixes",
)
_POSITIVE_INT_OVERRIDE_FIELDS = (
    "github_webhook_debounce_seconds",
    "max_concurrent_runs",
    "stale_run_timeout_seconds",
    "pr_lock_ttl_seconds",
    "max_retry_attempts",
    "retry_backoff_base_seconds",
    "retry_backoff_max_seconds",
)
_NON_NEGATIVE_INT_OVERRIDE_FIELDS = ("max_autofix_per_pr",)


@dataclass(frozen=True)
class RuntimeSettings:
    github_webhook_debounce_seconds: int = 60
    max_autofix_per_pr: int = 3
    max_concurrent_runs: int = 3
    stale_run_timeout_seconds: int = 900
    pr_lock_ttl_seconds: int = 900
    max_retry_attempts: int = 3
    retry_backoff_base_seconds: int = 30
    retry_backoff_max_seconds: int = 1800
    bot_logins: tuple[str, ...] = ()
    noise_comment_patterns: tuple[str, ...] = ()
    managed_repo_prefixes: tuple[str, ...] = ()
    autofix_comment_author: str = "software-factory[bot]"


class RuntimeSettingsEnvOverrides(BaseSettings):
    github_webhook_debounce_seconds: int | None = None
    max_autofix_per_pr: int | None = None
    max_concurrent_runs: int | None = None
    stale_run_timeout_seconds: int | None = None
    pr_lock_ttl_seconds: int | None = None
    max_retry_attempts: int | None = None
    retry_backoff_base_seconds: int | None = None
    retry_backoff_max_seconds: int | None = None
    bot_logins: tuple[str, ...] | None = None
    noise_comment_patterns: tuple[str, ...] | None = None
    managed_repo_prefixes: tuple[str, ...] | None = None
    autofix_comment_author: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _normalize_values(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        normalized = dict(data)
        for field_name in _LIST_OVERRIDE_FIELDS:
            if field_name in normalized and normalized[field_name] is not None:
                normalized[field_name] = _parse_list_value(normalized[field_name])
        if (
            "autofix_comment_author" in normalized
            and normalized["autofix_comment_author"] is not None
        ):
            normalized["autofix_comment_author"] = str(
                normalized["autofix_comment_author"]
            ).strip()
        return normalized

    @model_validator(mode="after")
    def _validate_numeric_ranges(self) -> RuntimeSettingsEnvOverrides:
        for field_name in _POSITIVE_INT_OVERRIDE_FIELDS:
            value = getattr(self, field_name)
            if value is not None and value <= 0:
                raise ValueError(f"{field_name} must be greater than 0")
        for field_name in _NON_NEGATIVE_INT_OVERRIDE_FIELDS:
            value = getattr(self, field_name)
            if value is not None and value < 0:
                raise ValueError(f"{field_name} must be non-negative")
        return self

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        enable_decoding=False,
        extra="ignore",
    )


def load_runtime_setting_rows(conn: sqlite3.Connection) -> dict[str, str]:
    try:
        rows = conn.execute(
            "SELECT key, value FROM app_feature_flags WHERE key LIKE 'runtime.%'"
        ).fetchall()
    except sqlite3.OperationalError:
        return {}

    return {str(row[0]): str(row[1]) for row in rows}


def resolve_runtime_settings(conn: sqlite3.Connection) -> RuntimeSettings:
    defaults = RuntimeSettings()
    overrides = RuntimeSettingsEnvOverrides()
    stored = load_runtime_setting_rows(conn)

    return RuntimeSettings(
        github_webhook_debounce_seconds=_resolve_positive_int(
            key=RUNTIME_GITHUB_WEBHOOK_DEBOUNCE_SECONDS_KEY,
            override=overrides.github_webhook_debounce_seconds,
            stored=stored,
            default=defaults.github_webhook_debounce_seconds,
        ),
        max_autofix_per_pr=_resolve_non_negative_int(
            key=RUNTIME_MAX_AUTOFIX_PER_PR_KEY,
            override=overrides.max_autofix_per_pr,
            stored=stored,
            default=defaults.max_autofix_per_pr,
        ),
        max_concurrent_runs=_resolve_positive_int(
            key=RUNTIME_MAX_CONCURRENT_RUNS_KEY,
            override=overrides.max_concurrent_runs,
            stored=stored,
            default=defaults.max_concurrent_runs,
        ),
        stale_run_timeout_seconds=_resolve_positive_int(
            key=RUNTIME_STALE_RUN_TIMEOUT_SECONDS_KEY,
            override=overrides.stale_run_timeout_seconds,
            stored=stored,
            default=defaults.stale_run_timeout_seconds,
        ),
        pr_lock_ttl_seconds=_resolve_positive_int(
            key=RUNTIME_PR_LOCK_TTL_SECONDS_KEY,
            override=overrides.pr_lock_ttl_seconds,
            stored=stored,
            default=defaults.pr_lock_ttl_seconds,
        ),
        max_retry_attempts=_resolve_positive_int(
            key=RUNTIME_MAX_RETRY_ATTEMPTS_KEY,
            override=overrides.max_retry_attempts,
            stored=stored,
            default=defaults.max_retry_attempts,
        ),
        retry_backoff_base_seconds=_resolve_positive_int(
            key=RUNTIME_RETRY_BACKOFF_BASE_SECONDS_KEY,
            override=overrides.retry_backoff_base_seconds,
            stored=stored,
            default=defaults.retry_backoff_base_seconds,
        ),
        retry_backoff_max_seconds=_resolve_positive_int(
            key=RUNTIME_RETRY_BACKOFF_MAX_SECONDS_KEY,
            override=overrides.retry_backoff_max_seconds,
            stored=stored,
            default=defaults.retry_backoff_max_seconds,
        ),
        bot_logins=_resolve_list_value(
            key=RUNTIME_BOT_LOGINS_KEY,
            override=overrides.bot_logins,
            stored=stored,
            default=defaults.bot_logins,
        ),
        noise_comment_patterns=_resolve_list_value(
            key=RUNTIME_NOISE_COMMENT_PATTERNS_KEY,
            override=overrides.noise_comment_patterns,
            stored=stored,
            default=defaults.noise_comment_patterns,
        ),
        managed_repo_prefixes=_resolve_list_value(
            key=RUNTIME_MANAGED_REPO_PREFIXES_KEY,
            override=overrides.managed_repo_prefixes,
            stored=stored,
            default=defaults.managed_repo_prefixes,
        ),
        autofix_comment_author=_resolve_text_value(
            key=RUNTIME_AUTOFIX_COMMENT_AUTHOR_KEY,
            override=overrides.autofix_comment_author,
            stored=stored,
            default=defaults.autofix_comment_author,
            allow_blank=True,
        ),
    )


def save_runtime_settings(
    conn: sqlite3.Connection,
    *,
    github_webhook_debounce_seconds: int,
    max_autofix_per_pr: int,
    max_concurrent_runs: int,
    stale_run_timeout_seconds: int,
    pr_lock_ttl_seconds: int,
    max_retry_attempts: int,
    retry_backoff_base_seconds: int,
    retry_backoff_max_seconds: int,
    bot_logins: tuple[str, ...] | list[str],
    noise_comment_patterns: tuple[str, ...] | list[str],
    managed_repo_prefixes: tuple[str, ...] | list[str],
    autofix_comment_author: str,
) -> None:
    values: list[tuple[str, str]] = [
        (
            RUNTIME_GITHUB_WEBHOOK_DEBOUNCE_SECONDS_KEY,
            str(max(1, int(github_webhook_debounce_seconds))),
        ),
        (RUNTIME_MAX_AUTOFIX_PER_PR_KEY, str(max(0, int(max_autofix_per_pr)))),
        (RUNTIME_MAX_CONCURRENT_RUNS_KEY, str(max(1, int(max_concurrent_runs)))),
        (
            RUNTIME_STALE_RUN_TIMEOUT_SECONDS_KEY,
            str(max(1, int(stale_run_timeout_seconds))),
        ),
        (RUNTIME_PR_LOCK_TTL_SECONDS_KEY, str(max(1, int(pr_lock_ttl_seconds)))),
        (RUNTIME_MAX_RETRY_ATTEMPTS_KEY, str(max(1, int(max_retry_attempts)))),
        (
            RUNTIME_RETRY_BACKOFF_BASE_SECONDS_KEY,
            str(max(1, int(retry_backoff_base_seconds))),
        ),
        (
            RUNTIME_RETRY_BACKOFF_MAX_SECONDS_KEY,
            str(max(1, int(retry_backoff_max_seconds))),
        ),
        (RUNTIME_BOT_LOGINS_KEY, _serialize_list_value(bot_logins)),
        (
            RUNTIME_NOISE_COMMENT_PATTERNS_KEY,
            _serialize_list_value(noise_comment_patterns),
        ),
        (
            RUNTIME_MANAGED_REPO_PREFIXES_KEY,
            _serialize_list_value(managed_repo_prefixes),
        ),
        (RUNTIME_AUTOFIX_COMMENT_AUTHOR_KEY, str(autofix_comment_author).strip()),
    ]

    conn.executemany(
        """
        INSERT INTO app_feature_flags (key, value)
        VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP
        """,
        values,
    )
    conn.commit()


def build_runtime_settings_context(conn: sqlite3.Connection) -> Mapping[str, Any]:
    runtime_settings = resolve_runtime_settings(conn)
    return {
        "github_webhook_debounce_seconds": str(
            runtime_settings.github_webhook_debounce_seconds
        ),
        "max_autofix_per_pr": str(runtime_settings.max_autofix_per_pr),
        "max_concurrent_runs": str(runtime_settings.max_concurrent_runs),
        "stale_run_timeout_seconds": str(runtime_settings.stale_run_timeout_seconds),
        "pr_lock_ttl_seconds": str(runtime_settings.pr_lock_ttl_seconds),
        "max_retry_attempts": str(runtime_settings.max_retry_attempts),
        "retry_backoff_base_seconds": str(runtime_settings.retry_backoff_base_seconds),
        "retry_backoff_max_seconds": str(runtime_settings.retry_backoff_max_seconds),
        "bot_logins_text": "\n".join(runtime_settings.bot_logins),
        "noise_comment_patterns_text": "\n".join(
            runtime_settings.noise_comment_patterns
        ),
        "managed_repo_prefixes_text": "\n".join(runtime_settings.managed_repo_prefixes),
        "autofix_comment_author": runtime_settings.autofix_comment_author,
    }


def parse_settings_list_form_value(value: str | None) -> tuple[str, ...]:
    if value is None:
        return ()
    items = [line.strip() for line in str(value).splitlines()]
    return tuple(item for item in items if item)


def _resolve_positive_int(
    *,
    key: str,
    override: int | None,
    stored: Mapping[str, str],
    default: int,
) -> int:
    if override is not None:
        return override
    raw_value = stored.get(key)
    if raw_value is None:
        return default
    try:
        value = int(str(raw_value).strip())
    except (TypeError, ValueError):
        _log_invalid_db_value(key, raw_value, default)
        return default
    if value <= 0:
        _log_invalid_db_value(key, raw_value, default)
        return default
    return value


def _resolve_non_negative_int(
    *,
    key: str,
    override: int | None,
    stored: Mapping[str, str],
    default: int,
) -> int:
    if override is not None:
        return override
    raw_value = stored.get(key)
    if raw_value is None:
        return default
    try:
        value = int(str(raw_value).strip())
    except (TypeError, ValueError):
        _log_invalid_db_value(key, raw_value, default)
        return default
    if value < 0:
        _log_invalid_db_value(key, raw_value, default)
        return default
    return value


def _resolve_list_value(
    *,
    key: str,
    override: tuple[str, ...] | None,
    stored: Mapping[str, str],
    default: tuple[str, ...],
) -> tuple[str, ...]:
    if override is not None:
        return override
    raw_value = stored.get(key)
    if raw_value is None:
        return default
    try:
        return _parse_list_value(raw_value)
    except (TypeError, ValueError, json.JSONDecodeError):
        _log_invalid_db_value(key, raw_value, list(default))
        return default


def _resolve_text_value(
    *,
    key: str,
    override: str | None,
    stored: Mapping[str, str],
    default: str,
    allow_blank: bool = False,
) -> str:
    if override is not None:
        return override
    raw_value = stored.get(key)
    if raw_value is None:
        return default
    text = str(raw_value).strip()
    if not text and not allow_blank:
        return default
    return text


def _parse_list_value(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return ()
        if text.startswith("["):
            decoded = json.loads(text)
            return _parse_list_value(decoded)
        items = [item.strip() for item in text.split(",")]
        return tuple(item for item in items if item)
    if isinstance(value, (list, tuple, set)):
        parsed_items: list[str] = []
        for item in value:
            if item is None:
                continue
            text = str(item).strip()
            if text:
                parsed_items.append(text)
        return tuple(parsed_items)
    text = str(value).strip()
    return (text,) if text else ()


def _serialize_list_value(value: tuple[str, ...] | list[str]) -> str:
    normalized = [str(item).strip() for item in value if str(item).strip()]
    return json.dumps(normalized)


def _log_invalid_db_value(key: str, value: Any, default: Any) -> None:
    _LOG.warning(
        "invalid runtime setting for %s: %r; falling back to %r",
        key,
        value,
        default,
    )
