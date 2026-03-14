from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Mapping

from app.config import get_settings

FEATURE_FLAG_OPENHANDS_ENABLED_KEY = "agent.openhands.enabled"
FEATURE_FLAG_LEGACY_ENABLED_KEY = "agent.legacy.enabled"
FEATURE_FLAG_OPENHANDS_COMMAND_KEY = "agent.openhands.command"
FEATURE_FLAG_OPENHANDS_TIMEOUT_KEY = "agent.openhands.command_timeout_seconds"
FEATURE_FLAG_OPENHANDS_WORKTREE_DIR_KEY = "agent.openhands.worktree_base_dir"

OPENHANDS_AGENT_MODE = "openhands"
LEGACY_AGENT_MODE = "legacy"


@dataclass(frozen=True)
class AgentFeatureFlags:
    agent_sdks: tuple[str, ...]
    openhands_command: str
    openhands_command_timeout_seconds: int
    openhands_worktree_base_dir: str


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
        openhands_command_timeout_seconds=settings.openhands_command_timeout_seconds,
        openhands_worktree_base_dir=
        settings.openhands_worktree_base_dir.strip() or ".software-factory-worktrees",
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
    legacy_enabled = _coerce_bool(
        raw_flags.get(FEATURE_FLAG_LEGACY_ENABLED_KEY),
        _feature_flag_default_enabled(LEGACY_AGENT_MODE, settings.agent_sdks),
    )

    if not openhands_enabled and not legacy_enabled:
        legacy_enabled = True

    modes: list[str] = []
    if openhands_enabled:
        modes.append(OPENHANDS_AGENT_MODE)
    if legacy_enabled:
        modes.append(LEGACY_AGENT_MODE)

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

    return AgentFeatureFlags(
        agent_sdks=tuple(modes),
        openhands_command=openhands_command,
        openhands_command_timeout_seconds=openhands_timeout,
        openhands_worktree_base_dir=worktree_dir,
    )


def save_agent_feature_flags(
    conn: sqlite3.Connection,
    *,
    openhands_enabled: bool,
    legacy_enabled: bool,
    openhands_command: str,
    openhands_command_timeout_seconds: int,
    openhands_worktree_base_dir: str,
) -> None:
    values: list[tuple[str, str]] = [
        (FEATURE_FLAG_OPENHANDS_ENABLED_KEY, "1" if openhands_enabled else "0"),
        (FEATURE_FLAG_LEGACY_ENABLED_KEY, "1" if legacy_enabled else "0"),
        (FEATURE_FLAG_OPENHANDS_COMMAND_KEY, openhands_command.strip()),
        (
            FEATURE_FLAG_OPENHANDS_TIMEOUT_KEY,
            str(max(1, int(openhands_command_timeout_seconds))),
        ),
        (
            FEATURE_FLAG_OPENHANDS_WORKTREE_DIR_KEY,
            openhands_worktree_base_dir.strip() or ".software-factory-worktrees",
        ),
    ]

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
        "agent_legacy_enabled": LEGACY_AGENT_MODE in flags.agent_sdks,
        "openhands_command": flags.openhands_command,
        "openhands_command_timeout_seconds": str(flags.openhands_command_timeout_seconds),
        "openhands_worktree_base_dir": flags.openhands_worktree_base_dir,
        "default_agent_sdks": ",".join(default_flags.agent_sdks),
    }


def _feature_flag_default_enabled(mode: str, current_modes: tuple[str, ...]) -> bool:
    return mode in {value.strip().lower() for value in current_modes}


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
