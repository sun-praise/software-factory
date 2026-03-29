from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Mapping

from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


FEATURE_FLAG_AGENT_SDKS_KEY = "agent.sdks"
FEATURE_FLAG_RALPH_ENABLED_KEY = "agent.ralph.enabled"
FEATURE_FLAG_RALPH_COMMAND_KEY = "agent.ralph.command"
FEATURE_FLAG_RALPH_TIMEOUT_KEY = "agent.ralph.command_timeout_seconds"
FEATURE_FLAG_OPENHANDS_ENABLED_KEY = "agent.openhands.enabled"
FEATURE_FLAG_OPENHANDS_COMMAND_KEY = "agent.openhands.command"
FEATURE_FLAG_OPENHANDS_TIMEOUT_KEY = "agent.openhands.command_timeout_seconds"
FEATURE_FLAG_OPENHANDS_WORKTREE_DIR_KEY = "agent.openhands.worktree_base_dir"
FEATURE_FLAG_CLAUDE_AGENT_ENABLED_KEY = "agent.claude_agent.enabled"
FEATURE_FLAG_CLAUDE_AGENT_COMMAND_KEY = "agent.claude_agent.command"
FEATURE_FLAG_CLAUDE_AGENT_PROVIDER_KEY = "agent.claude_agent.provider"
FEATURE_FLAG_CLAUDE_AGENT_BASE_URL_KEY = "agent.claude_agent.base_url"
FEATURE_FLAG_CLAUDE_AGENT_MODEL_KEY = "agent.claude_agent.model"
FEATURE_FLAG_CLAUDE_AGENT_RUNTIME_KEY = "agent.claude_agent.runtime"
FEATURE_FLAG_CLAUDE_AGENT_CONTAINER_IMAGE_KEY = "agent.claude_agent.container_image"
FEATURE_FLAG_CLAUDE_AGENT_TIMEOUT_KEY = "agent.claude_agent.command_timeout_seconds"
FEATURE_FLAG_CLAUDE_AGENT_WORKTREE_DIR_KEY = "agent.claude_agent.worktree_base_dir"
FEATURE_FLAG_LEGACY_ENABLED_KEY = "agent.legacy.enabled"

RALPH_AGENT_MODE = "ralph"
OPENHANDS_AGENT_MODE = "openhands"
CLAUDE_AGENT_MODE = "claude_agent_sdk"
LEGACY_AGENT_MODE = "legacy"
CLAUDE_AGENT_PROVIDER_ZHIPU = "zhipu"
CLAUDE_AGENT_PROVIDER_OPENROUTER = "openrouter"
CLAUDE_AGENT_PROVIDER_DEEPSEEK = "deepseek"
CLAUDE_AGENT_RUNTIME_HOST = "host"
CLAUDE_AGENT_RUNTIME_DOCKER = "docker"

_DEFAULT_AGENT_SDKS = (CLAUDE_AGENT_MODE, OPENHANDS_AGENT_MODE)
_DEFAULT_RALPH_COMMAND = "ralph"
_DEFAULT_OPENHANDS_COMMAND = "openhands"
_DEFAULT_CLAUDE_AGENT_COMMAND = "claude"
_DEFAULT_CLAUDE_AGENT_PROVIDER = CLAUDE_AGENT_PROVIDER_ZHIPU
_DEFAULT_CLAUDE_AGENT_BASE_URL = "https://open.bigmodel.cn/api/anthropic"
_DEFAULT_CLAUDE_AGENT_MODEL = "glm-5"
_DEFAULT_CLAUDE_AGENT_RUNTIME = CLAUDE_AGENT_RUNTIME_HOST
_DEFAULT_CLAUDE_AGENT_CONTAINER_IMAGE = "software-factory/claude-agent:latest"
_DEFAULT_RALPH_COMMAND_TIMEOUT_SECONDS = 1800
_DEFAULT_OPENHANDS_COMMAND_TIMEOUT_SECONDS = 600
_DEFAULT_CLAUDE_AGENT_COMMAND_TIMEOUT_SECONDS = 1800
_DEFAULT_AGENT_WORKTREE_BASE_DIR = ".software-factory-worktrees"
_TEXT_OVERRIDE_FIELDS = (
    "ralph_command",
    "openhands_command",
    "openhands_worktree_base_dir",
    "claude_agent_command",
    "claude_agent_provider",
    "claude_agent_base_url",
    "claude_agent_model",
    "claude_agent_runtime",
    "claude_agent_container_image",
    "claude_agent_worktree_base_dir",
)
_POSITIVE_INT_OVERRIDE_FIELDS = (
    "ralph_command_timeout_seconds",
    "openhands_command_timeout_seconds",
    "claude_agent_command_timeout_seconds",
)


@dataclass(frozen=True)
class AgentFeatureFlags:
    agent_sdks: tuple[str, ...]
    ralph_command: str
    ralph_command_timeout_seconds: int
    openhands_command: str
    openhands_command_timeout_seconds: int
    openhands_worktree_base_dir: str
    claude_agent_command: str
    claude_agent_provider: str
    claude_agent_base_url: str
    claude_agent_model: str
    claude_agent_runtime: str
    claude_agent_container_image: str
    claude_agent_command_timeout_seconds: int
    claude_agent_worktree_base_dir: str


class AgentFeatureFlagEnvOverrides(BaseSettings):
    agent_sdks: tuple[str, ...] | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "AGENT_SDKS",
            "CLAUDE_AGENT_SDKS",
        ),
    )
    ralph_command: str | None = None
    ralph_command_timeout_seconds: int | None = None
    openhands_command: str | None = None
    openhands_command_timeout_seconds: int | None = None
    openhands_worktree_base_dir: str | None = None
    claude_agent_command: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "CLAUDE_AGENT_COMMAND",
            "CLAUDE_AGENT_SDK_COMMAND",
        ),
    )
    claude_agent_provider: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "CLAUDE_AGENT_PROVIDER",
            "CLAUDE_AGENT_SDK_PROVIDER",
        ),
    )
    claude_agent_base_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "CLAUDE_AGENT_BASE_URL",
            "CLAUDE_AGENT_SDK_BASE_URL",
        ),
    )
    claude_agent_model: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "CLAUDE_AGENT_MODEL",
            "CLAUDE_AGENT_SDK_MODEL",
        ),
    )
    claude_agent_runtime: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "CLAUDE_AGENT_RUNTIME",
            "CLAUDE_AGENT_SDK_RUNTIME",
        ),
    )
    claude_agent_container_image: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "CLAUDE_AGENT_CONTAINER_IMAGE",
            "CLAUDE_AGENT_SDK_CONTAINER_IMAGE",
        ),
    )
    claude_agent_command_timeout_seconds: int | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "CLAUDE_AGENT_COMMAND_TIMEOUT_SECONDS",
            "CLAUDE_AGENT_SDK_COMMAND_TIMEOUT_SECONDS",
        ),
    )
    claude_agent_worktree_base_dir: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "CLAUDE_AGENT_WORKTREE_BASE_DIR",
            "CLAUDE_AGENT_SDK_WORKTREE_BASE_DIR",
        ),
    )

    @field_validator("agent_sdks", mode="before")
    @classmethod
    def _parse_agent_sdks(cls, value: Any) -> tuple[str, ...] | None:
        if value is None:
            return None
        return _parse_agent_modes(value)

    @model_validator(mode="before")
    @classmethod
    def _normalize_values(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        normalized = dict(data)
        for field_name in _TEXT_OVERRIDE_FIELDS:
            if field_name in normalized and normalized[field_name] is not None:
                normalized[field_name] = str(normalized[field_name]).strip()
        return normalized

    @model_validator(mode="after")
    def _validate_values(self) -> AgentFeatureFlagEnvOverrides:
        if self.agent_sdks is not None:
            self.agent_sdks = _normalize_agent_modes(self.agent_sdks)
        if self.claude_agent_provider is not None:
            self.claude_agent_provider = _normalize_provider(self.claude_agent_provider)
        if self.claude_agent_runtime is not None:
            self.claude_agent_runtime = _normalize_runtime(self.claude_agent_runtime)
        for field_name in _POSITIVE_INT_OVERRIDE_FIELDS:
            value = getattr(self, field_name)
            if value is not None and value <= 0:
                raise ValueError(f"{field_name} must be greater than 0")
        return self

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        enable_decoding=False,
        extra="ignore",
    )


def load_agent_feature_flags(conn: sqlite3.Connection) -> dict[str, str]:
    try:
        rows = conn.execute(
            "SELECT key, value FROM app_feature_flags WHERE key LIKE 'agent.%'"
        ).fetchall()
    except sqlite3.OperationalError:
        return {}

    return {str(key): str(value) for key, value in rows}


def _build_default_agent_feature_flags(
    env_overrides: AgentFeatureFlagEnvOverrides,
) -> AgentFeatureFlags:
    defaults = _get_code_default_agent_feature_flags()
    return AgentFeatureFlags(
        agent_sdks=env_overrides.agent_sdks or defaults.agent_sdks,
        ralph_command=_resolve_override_or_default(
            env_overrides.ralph_command,
            defaults.ralph_command,
        ),
        ralph_command_timeout_seconds=(
            env_overrides.ralph_command_timeout_seconds
            or defaults.ralph_command_timeout_seconds
        ),
        openhands_command=_resolve_override_or_default(
            env_overrides.openhands_command,
            defaults.openhands_command,
        ),
        openhands_command_timeout_seconds=(
            env_overrides.openhands_command_timeout_seconds
            or defaults.openhands_command_timeout_seconds
        ),
        openhands_worktree_base_dir=_resolve_override_or_default(
            env_overrides.openhands_worktree_base_dir,
            defaults.openhands_worktree_base_dir,
        ),
        claude_agent_command=_resolve_override_or_default(
            env_overrides.claude_agent_command,
            defaults.claude_agent_command,
        ),
        claude_agent_provider=(
            env_overrides.claude_agent_provider or defaults.claude_agent_provider
        ),
        claude_agent_base_url=_resolve_override_or_default(
            env_overrides.claude_agent_base_url,
            defaults.claude_agent_base_url,
        ),
        claude_agent_model=_resolve_override_or_default(
            env_overrides.claude_agent_model,
            defaults.claude_agent_model,
        ),
        claude_agent_runtime=(
            env_overrides.claude_agent_runtime or defaults.claude_agent_runtime
        ),
        claude_agent_container_image=_resolve_override_or_default(
            env_overrides.claude_agent_container_image,
            defaults.claude_agent_container_image,
            allow_blank=True,
        ),
        claude_agent_command_timeout_seconds=(
            env_overrides.claude_agent_command_timeout_seconds
            or defaults.claude_agent_command_timeout_seconds
        ),
        claude_agent_worktree_base_dir=_resolve_override_or_default(
            env_overrides.claude_agent_worktree_base_dir,
            defaults.claude_agent_worktree_base_dir,
        ),
    )


def get_default_agent_feature_flags() -> AgentFeatureFlags:
    env_overrides = get_agent_feature_flag_env_overrides()
    return _build_default_agent_feature_flags(env_overrides)


def resolve_agent_feature_flags(conn: sqlite3.Connection) -> AgentFeatureFlags:
    raw_flags = load_agent_feature_flags(conn)
    env_overrides = get_agent_feature_flag_env_overrides()
    defaults = _build_default_agent_feature_flags(env_overrides)

    return _resolve_agent_feature_flags_from_sources(
        raw_flags=raw_flags,
        defaults=defaults,
        env_overrides=env_overrides,
    )


def _resolve_agent_feature_flags_from_sources(
    *,
    raw_flags: Mapping[str, str],
    defaults: AgentFeatureFlags,
    env_overrides: AgentFeatureFlagEnvOverrides,
) -> AgentFeatureFlags:
    return AgentFeatureFlags(
        agent_sdks=_resolve_agent_sdks(
            env_override=env_overrides.agent_sdks,
            raw_flags=raw_flags,
            default_modes=defaults.agent_sdks,
        ),
        ralph_command=_resolve_text_value(
            key=FEATURE_FLAG_RALPH_COMMAND_KEY,
            override=env_overrides.ralph_command,
            raw_flags=raw_flags,
            default=defaults.ralph_command,
        ),
        ralph_command_timeout_seconds=_resolve_positive_int_value(
            key=FEATURE_FLAG_RALPH_TIMEOUT_KEY,
            override=env_overrides.ralph_command_timeout_seconds,
            raw_flags=raw_flags,
            default=defaults.ralph_command_timeout_seconds,
        ),
        openhands_command=_resolve_text_value(
            key=FEATURE_FLAG_OPENHANDS_COMMAND_KEY,
            override=env_overrides.openhands_command,
            raw_flags=raw_flags,
            default=defaults.openhands_command,
        ),
        openhands_command_timeout_seconds=_resolve_positive_int_value(
            key=FEATURE_FLAG_OPENHANDS_TIMEOUT_KEY,
            override=env_overrides.openhands_command_timeout_seconds,
            raw_flags=raw_flags,
            default=defaults.openhands_command_timeout_seconds,
        ),
        openhands_worktree_base_dir=_resolve_text_value(
            key=FEATURE_FLAG_OPENHANDS_WORKTREE_DIR_KEY,
            override=env_overrides.openhands_worktree_base_dir,
            raw_flags=raw_flags,
            default=defaults.openhands_worktree_base_dir,
        ),
        claude_agent_command=_resolve_text_value(
            key=FEATURE_FLAG_CLAUDE_AGENT_COMMAND_KEY,
            override=env_overrides.claude_agent_command,
            raw_flags=raw_flags,
            default=defaults.claude_agent_command,
        ),
        claude_agent_provider=_resolve_provider_value(
            key=FEATURE_FLAG_CLAUDE_AGENT_PROVIDER_KEY,
            override=env_overrides.claude_agent_provider,
            raw_flags=raw_flags,
            default=defaults.claude_agent_provider,
        ),
        claude_agent_base_url=_resolve_text_value(
            key=FEATURE_FLAG_CLAUDE_AGENT_BASE_URL_KEY,
            override=env_overrides.claude_agent_base_url,
            raw_flags=raw_flags,
            default=defaults.claude_agent_base_url,
        ),
        claude_agent_model=_resolve_text_value(
            key=FEATURE_FLAG_CLAUDE_AGENT_MODEL_KEY,
            override=env_overrides.claude_agent_model,
            raw_flags=raw_flags,
            default=defaults.claude_agent_model,
        ),
        claude_agent_runtime=_resolve_runtime_value(
            key=FEATURE_FLAG_CLAUDE_AGENT_RUNTIME_KEY,
            override=env_overrides.claude_agent_runtime,
            raw_flags=raw_flags,
            default=defaults.claude_agent_runtime,
        ),
        claude_agent_container_image=_resolve_text_value(
            key=FEATURE_FLAG_CLAUDE_AGENT_CONTAINER_IMAGE_KEY,
            override=env_overrides.claude_agent_container_image,
            raw_flags=raw_flags,
            default=defaults.claude_agent_container_image,
            allow_blank=True,
        ),
        claude_agent_command_timeout_seconds=_resolve_positive_int_value(
            key=FEATURE_FLAG_CLAUDE_AGENT_TIMEOUT_KEY,
            override=env_overrides.claude_agent_command_timeout_seconds,
            raw_flags=raw_flags,
            default=defaults.claude_agent_command_timeout_seconds,
        ),
        claude_agent_worktree_base_dir=_resolve_text_value(
            key=FEATURE_FLAG_CLAUDE_AGENT_WORKTREE_DIR_KEY,
            override=env_overrides.claude_agent_worktree_base_dir,
            raw_flags=raw_flags,
            default=defaults.claude_agent_worktree_base_dir,
        ),
    )


def save_agent_feature_flags(
    conn: sqlite3.Connection,
    *,
    flags: AgentFeatureFlags,
    legacy_enabled: bool | None = None,
) -> None:
    normalized_modes = _normalize_agent_modes(flags.agent_sdks)
    if not normalized_modes:
        normalized_modes = (CLAUDE_AGENT_MODE,)
    ralph_enabled = RALPH_AGENT_MODE in normalized_modes
    openhands_enabled = OPENHANDS_AGENT_MODE in normalized_modes
    claude_agent_enabled = CLAUDE_AGENT_MODE in normalized_modes
    # agent.legacy.enabled is intentionally mirrored from Claude Agent mode
    # for backward compatibility with older deployments that only recognized
    # a single "legacy" agent toggle.  The flag is always kept in sync so
    # that downgraded instances continue to see the correct enabled state.
    legacy_write_value = (
        claude_agent_enabled if legacy_enabled is None else legacy_enabled
    )
    values: list[tuple[str, str]] = [
        (FEATURE_FLAG_AGENT_SDKS_KEY, json.dumps(list(normalized_modes))),
        (FEATURE_FLAG_RALPH_ENABLED_KEY, "1" if ralph_enabled else "0"),
        (FEATURE_FLAG_OPENHANDS_ENABLED_KEY, "1" if openhands_enabled else "0"),
        (FEATURE_FLAG_CLAUDE_AGENT_ENABLED_KEY, "1" if claude_agent_enabled else "0"),
        (FEATURE_FLAG_RALPH_COMMAND_KEY, flags.ralph_command.strip()),
        (
            FEATURE_FLAG_RALPH_TIMEOUT_KEY,
            str(max(1, int(flags.ralph_command_timeout_seconds))),
        ),
        (FEATURE_FLAG_OPENHANDS_COMMAND_KEY, flags.openhands_command.strip()),
        (
            FEATURE_FLAG_OPENHANDS_TIMEOUT_KEY,
            str(max(1, int(flags.openhands_command_timeout_seconds))),
        ),
        (
            FEATURE_FLAG_OPENHANDS_WORKTREE_DIR_KEY,
            flags.openhands_worktree_base_dir.strip()
            or _DEFAULT_AGENT_WORKTREE_BASE_DIR,
        ),
        (FEATURE_FLAG_CLAUDE_AGENT_COMMAND_KEY, flags.claude_agent_command.strip()),
        (
            FEATURE_FLAG_CLAUDE_AGENT_PROVIDER_KEY,
            _normalize_provider(flags.claude_agent_provider),
        ),
        (
            FEATURE_FLAG_CLAUDE_AGENT_BASE_URL_KEY,
            flags.claude_agent_base_url.strip(),
        ),
        (
            FEATURE_FLAG_CLAUDE_AGENT_MODEL_KEY,
            flags.claude_agent_model.strip(),
        ),
        (
            FEATURE_FLAG_CLAUDE_AGENT_RUNTIME_KEY,
            _normalize_runtime(flags.claude_agent_runtime),
        ),
        (
            FEATURE_FLAG_CLAUDE_AGENT_CONTAINER_IMAGE_KEY,
            flags.claude_agent_container_image.strip(),
        ),
        (
            FEATURE_FLAG_CLAUDE_AGENT_TIMEOUT_KEY,
            str(max(1, int(flags.claude_agent_command_timeout_seconds))),
        ),
        (
            FEATURE_FLAG_CLAUDE_AGENT_WORKTREE_DIR_KEY,
            flags.claude_agent_worktree_base_dir.strip()
            or _DEFAULT_AGENT_WORKTREE_BASE_DIR,
        ),
        (FEATURE_FLAG_LEGACY_ENABLED_KEY, "1" if legacy_write_value else "0"),
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


def build_feature_flag_context(conn: sqlite3.Connection) -> Mapping[str, Any]:
    raw_flags = load_agent_feature_flags(conn)
    env_overrides = get_agent_feature_flag_env_overrides()
    default_flags = _build_default_agent_feature_flags(env_overrides)
    flags = _resolve_agent_feature_flags_from_sources(
        raw_flags=raw_flags,
        defaults=default_flags,
        env_overrides=env_overrides,
    )

    return {
        "agent_ralph_enabled": RALPH_AGENT_MODE in flags.agent_sdks,
        "agent_openhands_enabled": OPENHANDS_AGENT_MODE in flags.agent_sdks,
        "agent_claude_agent_enabled": CLAUDE_AGENT_MODE in flags.agent_sdks,
        "agent_primary_sdk": flags.agent_sdks[0]
        if flags.agent_sdks
        else CLAUDE_AGENT_MODE,
        "ralph_command": flags.ralph_command,
        "ralph_command_timeout_seconds": str(flags.ralph_command_timeout_seconds),
        "openhands_command": flags.openhands_command,
        "openhands_command_timeout_seconds": str(
            flags.openhands_command_timeout_seconds
        ),
        "openhands_worktree_base_dir": flags.openhands_worktree_base_dir,
        "claude_agent_command": flags.claude_agent_command,
        "claude_agent_provider": flags.claude_agent_provider,
        "claude_agent_base_url": flags.claude_agent_base_url,
        "claude_agent_model": flags.claude_agent_model,
        "claude_agent_runtime": flags.claude_agent_runtime,
        "claude_agent_container_image": flags.claude_agent_container_image,
        "claude_agent_command_timeout_seconds": str(
            flags.claude_agent_command_timeout_seconds
        ),
        "claude_agent_worktree_base_dir": flags.claude_agent_worktree_base_dir,
        "default_agent_sdks": ",".join(default_flags.agent_sdks),
    }


def build_selected_agent_sdks(
    primary_mode: str,
    *,
    ralph_enabled: bool,
    openhands_enabled: bool,
    claude_agent_enabled: bool,
) -> tuple[str, ...]:
    if not ralph_enabled and not openhands_enabled and not claude_agent_enabled:
        claude_agent_enabled = True
    preferred_modes = _normalize_agent_modes((primary_mode,))
    if not preferred_modes:
        preferred_modes = (CLAUDE_AGENT_MODE,)
    return tuple(
        _resolve_enabled_modes(
            preferred_modes=preferred_modes,
            ralph_enabled=ralph_enabled,
            openhands_enabled=openhands_enabled,
            claude_enabled=claude_agent_enabled,
        )
    )


def _get_code_default_agent_feature_flags() -> AgentFeatureFlags:
    return AgentFeatureFlags(
        agent_sdks=_DEFAULT_AGENT_SDKS,
        ralph_command=_DEFAULT_RALPH_COMMAND,
        ralph_command_timeout_seconds=_DEFAULT_RALPH_COMMAND_TIMEOUT_SECONDS,
        openhands_command=_DEFAULT_OPENHANDS_COMMAND,
        openhands_command_timeout_seconds=_DEFAULT_OPENHANDS_COMMAND_TIMEOUT_SECONDS,
        openhands_worktree_base_dir=_DEFAULT_AGENT_WORKTREE_BASE_DIR,
        claude_agent_command=_DEFAULT_CLAUDE_AGENT_COMMAND,
        claude_agent_provider=_DEFAULT_CLAUDE_AGENT_PROVIDER,
        claude_agent_base_url=_DEFAULT_CLAUDE_AGENT_BASE_URL,
        claude_agent_model=_DEFAULT_CLAUDE_AGENT_MODEL,
        claude_agent_runtime=_DEFAULT_CLAUDE_AGENT_RUNTIME,
        claude_agent_container_image=_DEFAULT_CLAUDE_AGENT_CONTAINER_IMAGE,
        claude_agent_command_timeout_seconds=(
            _DEFAULT_CLAUDE_AGENT_COMMAND_TIMEOUT_SECONDS
        ),
        claude_agent_worktree_base_dir=_DEFAULT_AGENT_WORKTREE_BASE_DIR,
    )


def _resolve_agent_sdks(
    *,
    env_override: tuple[str, ...] | None,
    raw_flags: Mapping[str, str],
    default_modes: tuple[str, ...],
) -> tuple[str, ...]:
    if env_override is not None:
        # Treat an explicitly blank env override the same as an unset value:
        # fall back to the configured defaults rather than allowing all agent
        # modes to disappear from the resolved runtime configuration.
        return env_override or default_modes

    raw_modes = raw_flags.get(FEATURE_FLAG_AGENT_SDKS_KEY)
    if raw_modes is not None:
        parsed_modes = _parse_agent_modes(raw_modes)
        normalized_modes = _normalize_agent_modes(parsed_modes)
        if normalized_modes:
            return normalized_modes

    ralph_enabled = _coerce_bool(
        raw_flags.get(FEATURE_FLAG_RALPH_ENABLED_KEY),
        _feature_flag_default_enabled(RALPH_AGENT_MODE, default_modes),
    )
    openhands_enabled = _coerce_bool(
        raw_flags.get(FEATURE_FLAG_OPENHANDS_ENABLED_KEY),
        _feature_flag_default_enabled(OPENHANDS_AGENT_MODE, default_modes),
    )
    claude_enabled = _coerce_bool(
        raw_flags.get(FEATURE_FLAG_CLAUDE_AGENT_ENABLED_KEY),
        _feature_flag_default_enabled(CLAUDE_AGENT_MODE, default_modes),
    )
    legacy_flag_present = FEATURE_FLAG_LEGACY_ENABLED_KEY in raw_flags
    legacy_enabled = _coerce_bool(
        raw_flags.get(
            FEATURE_FLAG_LEGACY_ENABLED_KEY, None if legacy_flag_present else "1"
        ),
        _feature_flag_default_enabled(LEGACY_AGENT_MODE, default_modes),
    )
    if legacy_flag_present:
        claude_enabled = legacy_enabled

    if not ralph_enabled and not openhands_enabled and not claude_enabled:
        claude_enabled = True

    return tuple(
        _resolve_enabled_modes(
            preferred_modes=default_modes,
            ralph_enabled=ralph_enabled,
            openhands_enabled=openhands_enabled,
            claude_enabled=claude_enabled,
        )
    )


def _resolve_override_or_default(
    override: str | None,
    default: str,
    *,
    allow_blank: bool = False,
) -> str:
    if override is None:
        return default
    text = str(override).strip()
    if not text and not allow_blank:
        return default
    return text


def _resolve_text_value(
    *,
    key: str,
    override: str | None,
    raw_flags: Mapping[str, str],
    default: str,
    allow_blank: bool = False,
) -> str:
    if override is not None:
        return _resolve_override_or_default(override, default, allow_blank=allow_blank)
    raw_value = raw_flags.get(key)
    if raw_value is None:
        return default
    return _resolve_override_or_default(raw_value, default, allow_blank=allow_blank)


def _resolve_positive_int_value(
    *,
    key: str,
    override: int | None,
    raw_flags: Mapping[str, str],
    default: int,
) -> int:
    if override is not None:
        return override
    value = _coerce_int(raw_flags.get(key), default)
    return value if value > 0 else default


def _resolve_provider_value(
    *,
    key: str,
    override: str | None,
    raw_flags: Mapping[str, str],
    default: str,
) -> str:
    return _resolve_normalized_value(
        key=key,
        override=override,
        raw_flags=raw_flags,
        default=default,
        normalizer=_normalize_provider,
    )


def _resolve_runtime_value(
    *,
    key: str,
    override: str | None,
    raw_flags: Mapping[str, str],
    default: str,
) -> str:
    return _resolve_normalized_value(
        key=key,
        override=override,
        raw_flags=raw_flags,
        default=default,
        normalizer=_normalize_runtime,
    )


def _resolve_normalized_value(
    *,
    key: str,
    override: str | None,
    raw_flags: Mapping[str, str],
    default: str,
    normalizer,
) -> str:
    if override is not None:
        normalized = normalizer(override)
        return normalized if normalized else default
    raw_value = raw_flags.get(key)
    if raw_value is None:
        return default
    normalized = normalizer(raw_value)
    return normalized if normalized else default


def _parse_agent_modes(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return ()
        if text.startswith("["):
            try:
                decoded = json.loads(text)
            except json.JSONDecodeError:
                return ()
            return _parse_agent_modes(decoded)
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


def _normalize_agent_modes(values: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    normalized_modes: list[str] = []
    for raw_mode in values:
        normalized = str(raw_mode).strip().lower()
        if normalized == LEGACY_AGENT_MODE:
            normalized = CLAUDE_AGENT_MODE
        if normalized not in {
            RALPH_AGENT_MODE,
            OPENHANDS_AGENT_MODE,
            CLAUDE_AGENT_MODE,
        }:
            continue
        if normalized in normalized_modes:
            continue
        normalized_modes.append(normalized)
    return tuple(normalized_modes)


@lru_cache
def get_agent_feature_flag_env_overrides() -> AgentFeatureFlagEnvOverrides:
    return AgentFeatureFlagEnvOverrides()


def _feature_flag_default_enabled(mode: str, current_modes: tuple[str, ...]) -> bool:
    return mode in {value.strip().lower() for value in current_modes}


def _resolve_enabled_modes(
    *,
    preferred_modes: tuple[str, ...],
    ralph_enabled: bool,
    openhands_enabled: bool,
    claude_enabled: bool,
) -> list[str]:
    ordered_modes = list(_normalize_agent_modes(preferred_modes))

    if CLAUDE_AGENT_MODE not in ordered_modes:
        ordered_modes.append(CLAUDE_AGENT_MODE)
    if OPENHANDS_AGENT_MODE not in ordered_modes:
        ordered_modes.append(OPENHANDS_AGENT_MODE)
    if RALPH_AGENT_MODE not in ordered_modes:
        ordered_modes.append(RALPH_AGENT_MODE)

    resolved: list[str] = []
    for mode in ordered_modes:
        if mode == CLAUDE_AGENT_MODE and claude_enabled:
            resolved.append(mode)
        if mode == OPENHANDS_AGENT_MODE and openhands_enabled:
            resolved.append(mode)
        if mode == RALPH_AGENT_MODE and ralph_enabled:
            resolved.append(mode)
    return resolved


def _coerce_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on", "enable", "enabled"}:
        return True
    if normalized in {"0", "false", "no", "off", "disable", "disabled"}:
        return False
    return default


def _coerce_int(value: str | None, default: int) -> int:
    if value is None:
        return default
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _normalize_runtime(value: str | None) -> str:
    normalized = str(value or "").strip().lower()
    if normalized == CLAUDE_AGENT_RUNTIME_DOCKER:
        return CLAUDE_AGENT_RUNTIME_DOCKER
    return CLAUDE_AGENT_RUNTIME_HOST


def _normalize_provider(value: str | None) -> str:
    normalized = str(value or "").strip().lower()
    if not normalized:
        return ""
    if normalized == CLAUDE_AGENT_PROVIDER_ZHIPU:
        return CLAUDE_AGENT_PROVIDER_ZHIPU
    if normalized == CLAUDE_AGENT_PROVIDER_DEEPSEEK:
        return CLAUDE_AGENT_PROVIDER_DEEPSEEK
    return CLAUDE_AGENT_PROVIDER_OPENROUTER
