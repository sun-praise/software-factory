from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Mapping

from app.config import get_settings

FEATURE_FLAG_OPENHANDS_ENABLED_KEY = "agent.openhands.enabled"
FEATURE_FLAG_OPENHANDS_COMMAND_KEY = "agent.openhands.command"
FEATURE_FLAG_OPENHANDS_TIMEOUT_KEY = "agent.openhands.command_timeout_seconds"
FEATURE_FLAG_OPENHANDS_WORKTREE_DIR_KEY = "agent.openhands.worktree_base_dir"
FEATURE_FLAG_CLAUDE_AGENT_ENABLED_KEY = "agent.claude_agent.enabled"
FEATURE_FLAG_CLAUDE_AGENT_COMMAND_KEY = "agent.claude_agent.command"
FEATURE_FLAG_CLAUDE_AGENT_RUNTIME_KEY = "agent.claude_agent.runtime"
FEATURE_FLAG_CLAUDE_AGENT_CONTAINER_IMAGE_KEY = "agent.claude_agent.container_image"
FEATURE_FLAG_CLAUDE_AGENT_TIMEOUT_KEY = "agent.claude_agent.command_timeout_seconds"
FEATURE_FLAG_CLAUDE_AGENT_WORKTREE_DIR_KEY = "agent.claude_agent.worktree_base_dir"
FEATURE_FLAG_LEGACY_ENABLED_KEY = "agent.legacy.enabled"

OPENHANDS_AGENT_MODE = "openhands"
CLAUDE_AGENT_MODE = "claude_agent_sdk"
LEGACY_AGENT_MODE = "legacy"
CLAUDE_AGENT_RUNTIME_HOST = "host"
CLAUDE_AGENT_RUNTIME_DOCKER = "docker"


@dataclass(frozen=True)
class AgentFeatureFlags:
    agent_sdks: tuple[str, ...]
    openhands_command: str
    openhands_command_timeout_seconds: int
    openhands_worktree_base_dir: str
    claude_agent_command: str
    claude_agent_runtime: str
    claude_agent_container_image: str
    claude_agent_command_timeout_seconds: int
    claude_agent_worktree_base_dir: str


def load_agent_feature_flags(
    conn: sqlite3.Connection,
) -> dict[str, str]:
    try:
        rows = conn.execute(
            "SELECT key, value FROM app_feature_flags"
        ).fetchall()
    except sqlite3.OperationalError:
        return {}

    return {str(key): str(value) for key, value in rows}


def get_default_agent_feature_flags() -> AgentFeatureFlags:
    settings = get_settings()
    return AgentFeatureFlags(
        agent_sdks=tuple(
            str(mode).strip().lower() for mode in settings.agent_sdks if str(mode).strip()
        ),
        openhands_command=settings.openhands_command.strip() or "openhands",
        claude_agent_command=settings.claude_agent_sdk_command.strip() or "claude",
        claude_agent_runtime=_normalize_runtime(settings.claude_agent_sdk_runtime),
        claude_agent_container_image=settings.claude_agent_sdk_container_image.strip(),
        openhands_command_timeout_seconds=settings.openhands_command_timeout_seconds,
        openhands_worktree_base_dir=
        settings.openhands_worktree_base_dir.strip() or ".software-factory-worktrees",
        claude_agent_command_timeout_seconds=(
            settings.claude_agent_sdk_command_timeout_seconds
        ),
        claude_agent_worktree_base_dir=(
            settings.claude_agent_sdk_worktree_base_dir.strip()
            or ".software-factory-worktrees"
        ),
    )


def resolve_agent_feature_flags(
    conn: sqlite3.Connection,
) -> AgentFeatureFlags:
    settings = get_default_agent_feature_flags()
    raw_flags = load_agent_feature_flags(conn)

    openhands_enabled = _coerce_bool(
        raw_flags.get(FEATURE_FLAG_OPENHANDS_ENABLED_KEY),
        _feature_flag_default_enabled(OPENHANDS_AGENT_MODE, settings.agent_sdks),
    )
    claude_enabled = _coerce_bool(
        raw_flags.get(FEATURE_FLAG_CLAUDE_AGENT_ENABLED_KEY),
        _feature_flag_default_enabled(CLAUDE_AGENT_MODE, settings.agent_sdks),
    )
    legacy_flag_present = "agent.legacy.enabled" in raw_flags
    # Keep the legacy flag as a compatibility alias for Claude mode so older
    # deployments keep their existing settings after the dual-SDK migration.
    legacy_enabled = _coerce_bool(
        raw_flags.get(FEATURE_FLAG_LEGACY_ENABLED_KEY, None if legacy_flag_present else "1"),
        _feature_flag_default_enabled(LEGACY_AGENT_MODE, settings.agent_sdks),
    )
    if legacy_flag_present:
        claude_enabled = legacy_enabled

    if not openhands_enabled and not claude_enabled:
        claude_enabled = True

    modes = _resolve_enabled_modes(
        preferred_modes=settings.agent_sdks,
        openhands_enabled=openhands_enabled,
        claude_enabled=claude_enabled,
    )

    openhands_command = raw_flags.get(
        FEATURE_FLAG_OPENHANDS_COMMAND_KEY,
        settings.openhands_command,
    ).strip() or settings.openhands_command
    openhands_timeout = _coerce_int(
        raw_flags.get(FEATURE_FLAG_OPENHANDS_TIMEOUT_KEY),
        settings.openhands_command_timeout_seconds,
    )
    if openhands_timeout <= 0:
        openhands_timeout = settings.openhands_command_timeout_seconds

    worktree_dir = raw_flags.get(
        FEATURE_FLAG_OPENHANDS_WORKTREE_DIR_KEY,
        settings.openhands_worktree_base_dir,
    ).strip() or settings.openhands_worktree_base_dir
    claude_command = raw_flags.get(
        FEATURE_FLAG_CLAUDE_AGENT_COMMAND_KEY,
        settings.claude_agent_command,
    ).strip() or settings.claude_agent_command
    claude_runtime = _normalize_runtime(
        raw_flags.get(FEATURE_FLAG_CLAUDE_AGENT_RUNTIME_KEY, settings.claude_agent_runtime)
    )
    claude_container_image = raw_flags.get(
        FEATURE_FLAG_CLAUDE_AGENT_CONTAINER_IMAGE_KEY,
        settings.claude_agent_container_image,
    ).strip()
    claude_timeout = _coerce_int(
        raw_flags.get(FEATURE_FLAG_CLAUDE_AGENT_TIMEOUT_KEY),
        settings.claude_agent_command_timeout_seconds,
    )
    if claude_timeout <= 0:
        claude_timeout = settings.claude_agent_command_timeout_seconds
    claude_worktree_dir = raw_flags.get(
        FEATURE_FLAG_CLAUDE_AGENT_WORKTREE_DIR_KEY,
        settings.claude_agent_worktree_base_dir,
    ).strip() or settings.claude_agent_worktree_base_dir

    return AgentFeatureFlags(
        agent_sdks=tuple(modes),
        openhands_command=openhands_command,
        openhands_command_timeout_seconds=openhands_timeout,
        openhands_worktree_base_dir=worktree_dir,
        claude_agent_command=claude_command,
        claude_agent_runtime=claude_runtime,
        claude_agent_container_image=claude_container_image,
        claude_agent_command_timeout_seconds=claude_timeout,
        claude_agent_worktree_base_dir=claude_worktree_dir,
    )


def save_agent_feature_flags(
    conn: sqlite3.Connection,
    *,
    openhands_enabled: bool,
    claude_agent_enabled: bool,
    openhands_command: str,
    openhands_command_timeout_seconds: int,
    openhands_worktree_base_dir: str,
    claude_agent_command: str,
    claude_agent_runtime: str,
    claude_agent_container_image: str,
    claude_agent_command_timeout_seconds: int,
    claude_agent_worktree_base_dir: str,
    legacy_enabled: bool | None = None,
) -> None:
    values: list[tuple[str, str]] = [
        (FEATURE_FLAG_OPENHANDS_ENABLED_KEY, "1" if openhands_enabled else "0"),
        (FEATURE_FLAG_CLAUDE_AGENT_ENABLED_KEY, "1" if claude_agent_enabled else "0"),
        (FEATURE_FLAG_OPENHANDS_COMMAND_KEY, openhands_command.strip()),
        (
            FEATURE_FLAG_OPENHANDS_TIMEOUT_KEY,
            str(max(1, int(openhands_command_timeout_seconds))),
        ),
        (
            FEATURE_FLAG_OPENHANDS_WORKTREE_DIR_KEY,
            openhands_worktree_base_dir.strip() or ".software-factory-worktrees",
        ),
        (FEATURE_FLAG_CLAUDE_AGENT_COMMAND_KEY, claude_agent_command.strip()),
        (
            FEATURE_FLAG_CLAUDE_AGENT_RUNTIME_KEY,
            _normalize_runtime(claude_agent_runtime),
        ),
        (
            FEATURE_FLAG_CLAUDE_AGENT_CONTAINER_IMAGE_KEY,
            claude_agent_container_image.strip(),
        ),
        (
            FEATURE_FLAG_CLAUDE_AGENT_TIMEOUT_KEY,
            str(max(1, int(claude_agent_command_timeout_seconds))),
        ),
        (
            FEATURE_FLAG_CLAUDE_AGENT_WORKTREE_DIR_KEY,
            claude_agent_worktree_base_dir.strip() or ".software-factory-worktrees",
        ),
    ]
    legacy_write_value = claude_agent_enabled if legacy_enabled is None else legacy_enabled
    values.append(
        (FEATURE_FLAG_LEGACY_ENABLED_KEY, "1" if legacy_write_value else "0"),
    )

    for key, value in values:
        conn.execute(
            """
            INSERT INTO app_feature_flags (key, value)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP
            """,
            (key, value),
        )
    conn.commit()


def build_feature_flag_context(conn: sqlite3.Connection) -> Mapping[str, Any]:
    default_flags = get_default_agent_feature_flags()
    flags = resolve_agent_feature_flags(conn)

    return {
        "agent_openhands_enabled": OPENHANDS_AGENT_MODE in flags.agent_sdks,
        "agent_claude_agent_enabled": CLAUDE_AGENT_MODE in flags.agent_sdks,
        "openhands_command": flags.openhands_command,
        "openhands_command_timeout_seconds": str(flags.openhands_command_timeout_seconds),
        "openhands_worktree_base_dir": flags.openhands_worktree_base_dir,
        "claude_agent_command": flags.claude_agent_command,
        "claude_agent_runtime": flags.claude_agent_runtime,
        "claude_agent_container_image": flags.claude_agent_container_image,
        "claude_agent_command_timeout_seconds": str(
            flags.claude_agent_command_timeout_seconds
        ),
        "claude_agent_worktree_base_dir": flags.claude_agent_worktree_base_dir,
        "default_agent_sdks": ",".join(default_flags.agent_sdks),
    }


def _feature_flag_default_enabled(mode: str, current_modes: tuple[str, ...]) -> bool:
    return mode in {value.strip().lower() for value in current_modes}


def _resolve_enabled_modes(
    *,
    preferred_modes: tuple[str, ...],
    openhands_enabled: bool,
    claude_enabled: bool,
) -> list[str]:
    ordered_modes: list[str] = []
    for raw_mode in preferred_modes:
        normalized = str(raw_mode).strip().lower()
        if normalized == LEGACY_AGENT_MODE:
            normalized = CLAUDE_AGENT_MODE
        if normalized not in {OPENHANDS_AGENT_MODE, CLAUDE_AGENT_MODE}:
            continue
        if normalized in ordered_modes:
            continue
        ordered_modes.append(normalized)

    if CLAUDE_AGENT_MODE not in ordered_modes:
        ordered_modes.append(CLAUDE_AGENT_MODE)
    if OPENHANDS_AGENT_MODE not in ordered_modes:
        ordered_modes.append(OPENHANDS_AGENT_MODE)

    resolved: list[str] = []
    for mode in ordered_modes:
        if mode == CLAUDE_AGENT_MODE and claude_enabled:
            resolved.append(mode)
        if mode == OPENHANDS_AGENT_MODE and openhands_enabled:
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
