from __future__ import annotations

import sqlite3

from app.models import SCHEMA_SQL
from app.services.feature_flags import (
    FEATURE_FLAG_AGENT_SDKS_KEY,
    FEATURE_FLAG_CLAUDE_AGENT_COMMAND_KEY,
    FEATURE_FLAG_CLAUDE_AGENT_PROVIDER_KEY,
    FEATURE_FLAG_CLAUDE_AGENT_RUNTIME_KEY,
    _DEFAULT_CLAUDE_AGENT_PROVIDER,
    _DEFAULT_CLAUDE_AGENT_RUNTIME,
    get_agent_feature_flag_env_overrides,
    build_selected_agent_sdks,
    resolve_agent_feature_flags,
)


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    return conn


def _clear_agent_env(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    get_agent_feature_flag_env_overrides.cache_clear()
    for key in (
        "AGENT_SDKS",
        "CLAUDE_AGENT_SDKS",
        "OPENHANDS_COMMAND",
        "OPENHANDS_COMMAND_TIMEOUT_SECONDS",
        "OPENHANDS_WORKTREE_BASE_DIR",
        "CLAUDE_AGENT_COMMAND",
        "CLAUDE_AGENT_PROVIDER",
        "CLAUDE_AGENT_BASE_URL",
        "CLAUDE_AGENT_MODEL",
        "CLAUDE_AGENT_RUNTIME",
        "CLAUDE_AGENT_CONTAINER_IMAGE",
        "CLAUDE_AGENT_COMMAND_TIMEOUT_SECONDS",
        "CLAUDE_AGENT_WORKTREE_BASE_DIR",
        "CLAUDE_AGENT_SDK_COMMAND",
        "CLAUDE_AGENT_SDK_PROVIDER",
        "CLAUDE_AGENT_SDK_BASE_URL",
        "CLAUDE_AGENT_SDK_MODEL",
        "CLAUDE_AGENT_SDK_RUNTIME",
        "CLAUDE_AGENT_SDK_CONTAINER_IMAGE",
        "CLAUDE_AGENT_SDK_COMMAND_TIMEOUT_SECONDS",
        "CLAUDE_AGENT_SDK_WORKTREE_BASE_DIR",
    ):
        monkeypatch.delenv(key, raising=False)


def test_resolve_agent_feature_flags_prefers_env_over_db(monkeypatch, tmp_path) -> None:
    _clear_agent_env(monkeypatch, tmp_path)
    conn = _make_conn()
    conn.execute(
        "INSERT INTO app_feature_flags (key, value) VALUES (?, ?)",
        (FEATURE_FLAG_CLAUDE_AGENT_COMMAND_KEY, "claude-from-db"),
    )
    conn.execute(
        "INSERT INTO app_feature_flags (key, value) VALUES (?, ?)",
        (FEATURE_FLAG_AGENT_SDKS_KEY, '["openhands", "claude_agent_sdk"]'),
    )
    conn.commit()

    monkeypatch.setenv("CLAUDE_AGENT_COMMAND", "claude-from-env")
    monkeypatch.setenv("AGENT_SDKS", "claude_agent_sdk,openhands")

    flags = resolve_agent_feature_flags(conn)

    assert flags.claude_agent_command == "claude-from-env"
    assert flags.agent_sdks == ("claude_agent_sdk", "openhands")


def test_resolve_agent_feature_flags_supports_legacy_env_aliases(
    monkeypatch, tmp_path
) -> None:
    _clear_agent_env(monkeypatch, tmp_path)
    conn = _make_conn()

    monkeypatch.setenv("CLAUDE_AGENT_SDK_COMMAND", "legacy-claude")
    monkeypatch.setenv("CLAUDE_AGENT_SDK_PROVIDER", "deepseek")

    flags = resolve_agent_feature_flags(conn)

    assert flags.claude_agent_command == "legacy-claude"
    assert flags.claude_agent_provider == "deepseek"


def test_resolve_agent_feature_flags_uses_db_agent_sdks_order(
    monkeypatch, tmp_path
) -> None:
    _clear_agent_env(monkeypatch, tmp_path)
    conn = _make_conn()
    conn.execute(
        "INSERT INTO app_feature_flags (key, value) VALUES (?, ?)",
        (FEATURE_FLAG_AGENT_SDKS_KEY, '["openhands", "claude_agent_sdk"]'),
    )
    conn.commit()

    flags = resolve_agent_feature_flags(conn)

    assert flags.agent_sdks == ("openhands", "claude_agent_sdk")


def test_build_selected_agent_sdks_respects_primary_mode() -> None:
    assert build_selected_agent_sdks(
        "openhands",
        openhands_enabled=True,
        claude_agent_enabled=True,
    ) == ("openhands", "claude_agent_sdk")
    assert build_selected_agent_sdks(
        "claude_agent_sdk",
        openhands_enabled=False,
        claude_agent_enabled=True,
    ) == ("claude_agent_sdk",)


def test_claude_agent_sdks_env_alias_accepted(monkeypatch, tmp_path) -> None:
    """CLAUDE_AGENT_SDKS is a legacy alias for AGENT_SDKS."""
    _clear_agent_env(monkeypatch, tmp_path)
    conn = _make_conn()

    monkeypatch.setenv("CLAUDE_AGENT_SDKS", "openhands,claude_agent_sdk")

    flags = resolve_agent_feature_flags(conn)
    assert flags.agent_sdks == ("openhands", "claude_agent_sdk")


def test_agent_sdks_canonical_env_preferred_over_legacy(monkeypatch, tmp_path) -> None:
    """When both AGENT_SDKS and CLAUDE_AGENT_SDKS are set, canonical wins."""
    _clear_agent_env(monkeypatch, tmp_path)
    conn = _make_conn()

    monkeypatch.setenv("AGENT_SDKS", "claude_agent_sdk")
    monkeypatch.setenv("CLAUDE_AGENT_SDKS", "openhands")

    flags = resolve_agent_feature_flags(conn)
    assert flags.agent_sdks == ("claude_agent_sdk",)


def test_invalid_agent_sdks_env_falls_back_to_defaults(monkeypatch, tmp_path) -> None:
    _clear_agent_env(monkeypatch, tmp_path)
    conn = _make_conn()

    monkeypatch.setenv("AGENT_SDKS", "unknown,other")

    flags = resolve_agent_feature_flags(conn)

    assert flags.agent_sdks == ("claude_agent_sdk", "openhands")


def test_blank_db_provider_falls_back_to_default(monkeypatch, tmp_path) -> None:
    """A blank provider value in the DB should fall back to the code default."""
    _clear_agent_env(monkeypatch, tmp_path)
    conn = _make_conn()
    conn.execute(
        "INSERT INTO app_feature_flags (key, value) VALUES (?, ?)",
        (FEATURE_FLAG_CLAUDE_AGENT_PROVIDER_KEY, ""),
    )
    conn.commit()

    flags = resolve_agent_feature_flags(conn)
    assert flags.claude_agent_provider == _DEFAULT_CLAUDE_AGENT_PROVIDER


def test_blank_db_runtime_falls_back_to_default(monkeypatch, tmp_path) -> None:
    """A blank runtime value in the DB should fall back to the code default."""
    _clear_agent_env(monkeypatch, tmp_path)
    conn = _make_conn()
    conn.execute(
        "INSERT INTO app_feature_flags (key, value) VALUES (?, ?)",
        (FEATURE_FLAG_CLAUDE_AGENT_RUNTIME_KEY, ""),
    )
    conn.commit()

    flags = resolve_agent_feature_flags(conn)
    assert flags.claude_agent_runtime == _DEFAULT_CLAUDE_AGENT_RUNTIME


def test_invalid_json_agent_sdks_in_db_falls_back_to_legacy_flags(
    monkeypatch, tmp_path
) -> None:
    _clear_agent_env(monkeypatch, tmp_path)
    conn = _make_conn()
    conn.execute(
        "INSERT INTO app_feature_flags (key, value) VALUES (?, ?)",
        (FEATURE_FLAG_AGENT_SDKS_KEY, "[invalid json"),
    )
    conn.execute(
        "INSERT INTO app_feature_flags (key, value) VALUES (?, ?)",
        ("agent.openhands.enabled", "1"),
    )
    conn.execute(
        "INSERT INTO app_feature_flags (key, value) VALUES (?, ?)",
        ("agent.claude_agent.enabled", "0"),
    )
    conn.commit()

    flags = resolve_agent_feature_flags(conn)

    assert flags.agent_sdks == ("openhands",)


def test_build_selected_agent_sdks_auto_enables_claude_when_both_disabled() -> None:
    """If both agent checkboxes are unchecked, Claude Agent SDK is forced on."""
    result = build_selected_agent_sdks(
        "claude_agent_sdk",
        openhands_enabled=False,
        claude_agent_enabled=False,
    )
    assert result == ("claude_agent_sdk",)


def test_build_selected_agent_sdks_auto_enables_claude_openhands_primary() -> None:
    """If both are disabled and OpenHands is primary, Claude is still added."""
    result = build_selected_agent_sdks(
        "openhands",
        openhands_enabled=False,
        claude_agent_enabled=False,
    )
    assert result == ("claude_agent_sdk",)
