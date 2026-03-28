from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from typing import Any, Mapping

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.config import Settings


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
RUNTIME_DB_PATH_KEY = "bootstrap.db_path"


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
_DB_OWNERSHIP = "db"
_ENV_ONLY_OWNERSHIP = "env_only"
_ENV_SOURCE = "env"
_DB_SOURCE = "db"
_DEFAULT_SOURCE = "default"
_MISSING = object()


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


@dataclass(frozen=True)
class RuntimeSettingSpec:
    key: str
    field_name: str
    label: str
    env_var: str
    ownership: str
    sensitive: bool
    default: Any
    value_type: str


@dataclass(frozen=True)
class RuntimeSettingDescription:
    key: str
    label: str
    ownership: str
    sensitive: bool
    env_var: str
    effective: Any
    source: str
    updated_at: str | None = None


_RUNTIME_DEFAULTS = RuntimeSettings()
_DEFAULT_DB_PATH = str(Settings.model_fields["db_path"].default).strip()
_RUNTIME_SETTING_SPECS = (
    RuntimeSettingSpec(
        key=RUNTIME_GITHUB_WEBHOOK_DEBOUNCE_SECONDS_KEY,
        field_name="github_webhook_debounce_seconds",
        label="GitHub webhook debounce window",
        env_var="GITHUB_WEBHOOK_DEBOUNCE_SECONDS",
        ownership=_DB_OWNERSHIP,
        sensitive=False,
        default=_RUNTIME_DEFAULTS.github_webhook_debounce_seconds,
        value_type="positive_int",
    ),
    RuntimeSettingSpec(
        key=RUNTIME_MAX_AUTOFIX_PER_PR_KEY,
        field_name="max_autofix_per_pr",
        label="Max autofix runs per PR",
        env_var="MAX_AUTOFIX_PER_PR",
        ownership=_DB_OWNERSHIP,
        sensitive=False,
        default=_RUNTIME_DEFAULTS.max_autofix_per_pr,
        value_type="non_negative_int",
    ),
    RuntimeSettingSpec(
        key=RUNTIME_MAX_CONCURRENT_RUNS_KEY,
        field_name="max_concurrent_runs",
        label="Max concurrent runs",
        env_var="MAX_CONCURRENT_RUNS",
        ownership=_DB_OWNERSHIP,
        sensitive=False,
        default=_RUNTIME_DEFAULTS.max_concurrent_runs,
        value_type="positive_int",
    ),
    RuntimeSettingSpec(
        key=RUNTIME_STALE_RUN_TIMEOUT_SECONDS_KEY,
        field_name="stale_run_timeout_seconds",
        label="Stale run timeout",
        env_var="STALE_RUN_TIMEOUT_SECONDS",
        ownership=_DB_OWNERSHIP,
        sensitive=False,
        default=_RUNTIME_DEFAULTS.stale_run_timeout_seconds,
        value_type="positive_int",
    ),
    RuntimeSettingSpec(
        key=RUNTIME_PR_LOCK_TTL_SECONDS_KEY,
        field_name="pr_lock_ttl_seconds",
        label="PR lock TTL",
        env_var="PR_LOCK_TTL_SECONDS",
        ownership=_DB_OWNERSHIP,
        sensitive=False,
        default=_RUNTIME_DEFAULTS.pr_lock_ttl_seconds,
        value_type="positive_int",
    ),
    RuntimeSettingSpec(
        key=RUNTIME_MAX_RETRY_ATTEMPTS_KEY,
        field_name="max_retry_attempts",
        label="Max retry attempts",
        env_var="MAX_RETRY_ATTEMPTS",
        ownership=_DB_OWNERSHIP,
        sensitive=False,
        default=_RUNTIME_DEFAULTS.max_retry_attempts,
        value_type="positive_int",
    ),
    RuntimeSettingSpec(
        key=RUNTIME_RETRY_BACKOFF_BASE_SECONDS_KEY,
        field_name="retry_backoff_base_seconds",
        label="Retry backoff base",
        env_var="RETRY_BACKOFF_BASE_SECONDS",
        ownership=_DB_OWNERSHIP,
        sensitive=False,
        default=_RUNTIME_DEFAULTS.retry_backoff_base_seconds,
        value_type="positive_int",
    ),
    RuntimeSettingSpec(
        key=RUNTIME_RETRY_BACKOFF_MAX_SECONDS_KEY,
        field_name="retry_backoff_max_seconds",
        label="Retry backoff max",
        env_var="RETRY_BACKOFF_MAX_SECONDS",
        ownership=_DB_OWNERSHIP,
        sensitive=False,
        default=_RUNTIME_DEFAULTS.retry_backoff_max_seconds,
        value_type="positive_int",
    ),
    RuntimeSettingSpec(
        key=RUNTIME_BOT_LOGINS_KEY,
        field_name="bot_logins",
        label="Bot logins",
        env_var="BOT_LOGINS",
        ownership=_DB_OWNERSHIP,
        sensitive=False,
        default=_RUNTIME_DEFAULTS.bot_logins,
        value_type="list",
    ),
    RuntimeSettingSpec(
        key=RUNTIME_NOISE_COMMENT_PATTERNS_KEY,
        field_name="noise_comment_patterns",
        label="Noise comment patterns",
        env_var="NOISE_COMMENT_PATTERNS",
        ownership=_DB_OWNERSHIP,
        sensitive=False,
        default=_RUNTIME_DEFAULTS.noise_comment_patterns,
        value_type="list",
    ),
    RuntimeSettingSpec(
        key=RUNTIME_MANAGED_REPO_PREFIXES_KEY,
        field_name="managed_repo_prefixes",
        label="Managed repo prefixes",
        env_var="MANAGED_REPO_PREFIXES",
        ownership=_DB_OWNERSHIP,
        sensitive=False,
        default=_RUNTIME_DEFAULTS.managed_repo_prefixes,
        value_type="list",
    ),
    RuntimeSettingSpec(
        key=RUNTIME_AUTOFIX_COMMENT_AUTHOR_KEY,
        field_name="autofix_comment_author",
        label="Autofix comment author",
        env_var="AUTOFIX_COMMENT_AUTHOR",
        ownership=_DB_OWNERSHIP,
        sensitive=False,
        default=_RUNTIME_DEFAULTS.autofix_comment_author,
        value_type="text",
    ),
    RuntimeSettingSpec(
        key=RUNTIME_DB_PATH_KEY,
        field_name="db_path",
        label="Database path",
        env_var="DB_PATH",
        ownership=_ENV_ONLY_OWNERSHIP,
        sensitive=False,
        default=_DEFAULT_DB_PATH,
        value_type="text",
    ),
)
_RUNTIME_SETTING_SPECS_BY_KEY = {spec.key: spec for spec in _RUNTIME_SETTING_SPECS}


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


class RuntimeEnvOnlyInspectSettings(BaseSettings):
    db_path: str = _DEFAULT_DB_PATH

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        enable_decoding=False,
        extra="ignore",
    )


def load_runtime_setting_records(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    try:
        rows = conn.execute(
            "SELECT key, value, updated_at FROM app_feature_flags WHERE key LIKE 'runtime.%'"
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    return {str(row["key"]): row for row in rows}


def load_runtime_setting_rows(conn: sqlite3.Connection) -> dict[str, str]:
    return {
        key: str(row["value"])
        for key, row in load_runtime_setting_records(conn).items()
    }


def describe_runtime_settings(
    conn: sqlite3.Connection,
) -> tuple[RuntimeSettingDescription, ...]:
    runtime_settings = resolve_runtime_settings(conn)
    overrides = RuntimeSettingsEnvOverrides()
    env_only_settings = RuntimeEnvOnlyInspectSettings()
    stored_records = load_runtime_setting_records(conn)

    descriptions: list[RuntimeSettingDescription] = []
    for spec in _RUNTIME_SETTING_SPECS:
        if spec.ownership == _DB_OWNERSHIP:
            override = getattr(overrides, spec.field_name)
            stored_row = stored_records.get(spec.key)
            effective = getattr(runtime_settings, spec.field_name)
            if override is not None:
                source = _ENV_SOURCE
            elif (
                stored_row is not None
                and _parse_db_runtime_value(spec, stored_row["value"]) is not _MISSING
            ):
                source = _DB_SOURCE
            else:
                source = _DEFAULT_SOURCE
            descriptions.append(
                RuntimeSettingDescription(
                    key=spec.key,
                    label=spec.label,
                    ownership=spec.ownership,
                    sensitive=spec.sensitive,
                    env_var=spec.env_var,
                    effective=effective,
                    source=source,
                    updated_at=(
                        str(stored_row["updated_at"])
                        if stored_row is not None
                        and stored_row["updated_at"] is not None
                        else None
                    ),
                )
            )
            continue

        descriptions.append(
            RuntimeSettingDescription(
                key=spec.key,
                label=spec.label,
                ownership=spec.ownership,
                sensitive=spec.sensitive,
                env_var=spec.env_var,
                effective=str(getattr(env_only_settings, spec.field_name)).strip(),
                source=(
                    _ENV_SOURCE
                    if spec.field_name in env_only_settings.model_fields_set
                    else _DEFAULT_SOURCE
                ),
                updated_at=None,
            )
        )

    return tuple(descriptions)


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
    changed_by: str = "system",
    change_source: str = "system",
) -> None:
    save_runtime_setting_values(
        conn,
        {
            RUNTIME_GITHUB_WEBHOOK_DEBOUNCE_SECONDS_KEY: str(
                max(1, int(github_webhook_debounce_seconds))
            ),
            RUNTIME_MAX_AUTOFIX_PER_PR_KEY: str(max(0, int(max_autofix_per_pr))),
            RUNTIME_MAX_CONCURRENT_RUNS_KEY: str(max(1, int(max_concurrent_runs))),
            RUNTIME_STALE_RUN_TIMEOUT_SECONDS_KEY: str(
                max(1, int(stale_run_timeout_seconds))
            ),
            RUNTIME_PR_LOCK_TTL_SECONDS_KEY: str(max(1, int(pr_lock_ttl_seconds))),
            RUNTIME_MAX_RETRY_ATTEMPTS_KEY: str(max(1, int(max_retry_attempts))),
            RUNTIME_RETRY_BACKOFF_BASE_SECONDS_KEY: str(
                max(1, int(retry_backoff_base_seconds))
            ),
            RUNTIME_RETRY_BACKOFF_MAX_SECONDS_KEY: str(
                max(1, int(retry_backoff_max_seconds))
            ),
            RUNTIME_BOT_LOGINS_KEY: _serialize_list_value(bot_logins),
            RUNTIME_NOISE_COMMENT_PATTERNS_KEY: _serialize_list_value(
                noise_comment_patterns
            ),
            RUNTIME_MANAGED_REPO_PREFIXES_KEY: _serialize_list_value(
                managed_repo_prefixes
            ),
            RUNTIME_AUTOFIX_COMMENT_AUTHOR_KEY: str(autofix_comment_author).strip(),
        },
        changed_by=changed_by,
        change_source=change_source,
    )


def save_runtime_setting_values(
    conn: sqlite3.Connection,
    values: Mapping[str, str],
    *,
    changed_by: str,
    change_source: str,
) -> None:
    stored_records = load_runtime_setting_records(conn)
    changed_rows: list[tuple[str, str | None, str, str, str]] = []
    normalized_values: list[tuple[str, str]] = []

    for key, value in values.items():
        spec = _RUNTIME_SETTING_SPECS_BY_KEY.get(key)
        if spec is None:
            raise ValueError(f"unknown runtime setting: {key}")
        if spec.ownership != _DB_OWNERSHIP:
            raise ValueError(
                f"runtime setting {key} is {spec.ownership} and cannot be persisted"
            )
        text_value = str(value)
        normalized_values.append((key, text_value))
        old_value = (
            str(stored_records[key]["value"])
            if key in stored_records and stored_records[key]["value"] is not None
            else None
        )
        if old_value != text_value:
            changed_rows.append((key, old_value, text_value, changed_by, change_source))

    conn.executemany(
        """
        INSERT INTO app_feature_flags (key, value)
        VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP
        """,
        normalized_values,
    )
    if changed_rows:
        conn.executemany(
            """
            INSERT INTO app_config_audit_log (key, old_value, new_value, changed_by, change_source)
            VALUES (?, ?, ?, ?, ?)
            """,
            changed_rows,
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


def _parse_db_runtime_value(spec: RuntimeSettingSpec, value: Any) -> Any:
    if spec.value_type == "positive_int":
        try:
            parsed = int(str(value).strip())
        except (TypeError, ValueError):
            return _MISSING
        return parsed if parsed > 0 else _MISSING
    if spec.value_type == "non_negative_int":
        try:
            parsed = int(str(value).strip())
        except (TypeError, ValueError):
            return _MISSING
        return parsed if parsed >= 0 else _MISSING
    if spec.value_type == "list":
        try:
            return _parse_list_value(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return _MISSING
    if spec.value_type == "text":
        return str(value).strip()
    raise ValueError(f"unsupported runtime setting value type: {spec.value_type}")


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
