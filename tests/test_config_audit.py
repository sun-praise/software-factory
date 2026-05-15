from __future__ import annotations

import sqlite3

from app.models import SCHEMA_SQL
from app.services.runtime_settings import (
    RUNTIME_MAX_RETRY_ATTEMPTS_KEY,
    RUNTIME_GITHUB_WEBHOOK_DEBOUNCE_SECONDS_KEY,
    RUNTIME_MAX_AUTOFIX_PER_PR_KEY,
    RUNTIME_BOT_LOGINS_KEY,
    RUNTIME_DB_PATH_KEY,
    RuntimeSettingsPayload,
    get_config_audit_history,
    rollback_config_value,
    resolve_runtime_settings,
    save_runtime_settings,
    save_runtime_setting_values,
)


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    return conn


def _clear_runtime_override_env(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    for key in (
        "DB_PATH",
        "GITHUB_WEBHOOK_DEBOUNCE_SECONDS",
        "MAX_AUTOFIX_PER_PR",
        "MAX_CONCURRENT_RUNS",
        "STALE_RUN_TIMEOUT_SECONDS",
        "PR_LOCK_TTL_SECONDS",
        "MAX_RETRY_ATTEMPTS",
        "RETRY_BACKOFF_BASE_SECONDS",
        "RETRY_BACKOFF_MAX_SECONDS",
        "BOT_LOGINS",
        "NOISE_COMMENT_PATTERNS",
        "MANAGED_REPO_PREFIXES",
        "AUTOFIX_COMMENT_AUTHOR",
    ):
        monkeypatch.delenv(key, raising=False)


def test_get_config_audit_history_returns_empty_when_no_changes(
    monkeypatch, tmp_path
) -> None:
    _clear_runtime_override_env(monkeypatch, tmp_path)
    conn = _make_conn()

    entries = get_config_audit_history(conn)

    assert entries == []


def test_get_config_audit_history_returns_entries_for_all_keys(
    monkeypatch, tmp_path
) -> None:
    _clear_runtime_override_env(monkeypatch, tmp_path)
    conn = _make_conn()

    save_runtime_setting_values(
        conn,
        {RUNTIME_MAX_RETRY_ATTEMPTS_KEY: "5"},
        changed_by="test",
        change_source="test",
    )
    save_runtime_setting_values(
        conn,
        {RUNTIME_GITHUB_WEBHOOK_DEBOUNCE_SECONDS_KEY: "120"},
        changed_by="test",
        change_source="test",
    )

    entries = get_config_audit_history(conn)

    assert len(entries) == 2
    assert entries[0].key == RUNTIME_GITHUB_WEBHOOK_DEBOUNCE_SECONDS_KEY
    assert entries[0].new_value == "120"
    assert entries[1].key == RUNTIME_MAX_RETRY_ATTEMPTS_KEY
    assert entries[1].new_value == "5"


def test_get_config_audit_history_filters_by_key(monkeypatch, tmp_path) -> None:
    _clear_runtime_override_env(monkeypatch, tmp_path)
    conn = _make_conn()

    save_runtime_setting_values(
        conn,
        {RUNTIME_MAX_RETRY_ATTEMPTS_KEY: "5"},
        changed_by="test",
        change_source="test",
    )
    save_runtime_setting_values(
        conn,
        {RUNTIME_GITHUB_WEBHOOK_DEBOUNCE_SECONDS_KEY: "120"},
        changed_by="test",
        change_source="test",
    )

    entries = get_config_audit_history(conn, key=RUNTIME_MAX_RETRY_ATTEMPTS_KEY)

    assert len(entries) == 1
    assert entries[0].key == RUNTIME_MAX_RETRY_ATTEMPTS_KEY


def test_get_config_audit_history_respects_limit(monkeypatch, tmp_path) -> None:
    _clear_runtime_override_env(monkeypatch, tmp_path)
    conn = _make_conn()

    for i in range(5):
        save_runtime_setting_values(
            conn,
            {RUNTIME_MAX_RETRY_ATTEMPTS_KEY: str(i + 1)},
            changed_by="test",
            change_source="test",
        )

    entries = get_config_audit_history(
        conn, key=RUNTIME_MAX_RETRY_ATTEMPTS_KEY, limit=3
    )

    assert len(entries) == 3


def test_get_config_audit_history_rejects_unknown_key(
    monkeypatch, tmp_path
) -> None:
    _clear_runtime_override_env(monkeypatch, tmp_path)
    conn = _make_conn()

    try:
        get_config_audit_history(conn, key="runtime.nonexistent")
    except ValueError as exc:
        assert "unknown runtime setting" in str(exc)
    else:
        raise AssertionError("expected ValueError for unknown key")


def test_rollback_config_value_restores_previous_value(
    monkeypatch, tmp_path
) -> None:
    _clear_runtime_override_env(monkeypatch, tmp_path)
    conn = _make_conn()

    save_runtime_setting_values(
        conn,
        {RUNTIME_MAX_RETRY_ATTEMPTS_KEY: "5"},
        changed_by="test",
        change_source="test",
    )
    save_runtime_setting_values(
        conn,
        {RUNTIME_MAX_RETRY_ATTEMPTS_KEY: "8"},
        changed_by="test",
        change_source="test",
    )

    entries = get_config_audit_history(
        conn, key=RUNTIME_MAX_RETRY_ATTEMPTS_KEY
    )
    second_change = entries[0]
    assert second_change.new_value == "8"

    rolled_back = rollback_config_value(
        conn,
        key=RUNTIME_MAX_RETRY_ATTEMPTS_KEY,
        target_audit_id=second_change.id,
        changed_by="operator",
        change_source="api.rollback",
    )

    assert rolled_back.old_value == "5"

    settings = resolve_runtime_settings(conn)
    assert settings.max_retry_attempts == 5

    audit_entries = get_config_audit_history(
        conn, key=RUNTIME_MAX_RETRY_ATTEMPTS_KEY
    )
    assert audit_entries[0].changed_by == "operator"
    assert audit_entries[0].change_source == "api.rollback"
    assert audit_entries[0].old_value == "8"
    assert audit_entries[0].new_value == "5"


def test_rollback_config_value_removes_key_when_old_value_is_none(
    monkeypatch, tmp_path
) -> None:
    _clear_runtime_override_env(monkeypatch, tmp_path)
    conn = _make_conn()

    save_runtime_setting_values(
        conn,
        {RUNTIME_MAX_AUTOFIX_PER_PR_KEY: "10"},
        changed_by="test",
        change_source="test",
    )

    entries = get_config_audit_history(
        conn, key=RUNTIME_MAX_AUTOFIX_PER_PR_KEY
    )
    first_change = entries[0]
    assert first_change.old_value is None
    assert first_change.new_value == "10"

    rollback_config_value(
        conn,
        key=RUNTIME_MAX_AUTOFIX_PER_PR_KEY,
        target_audit_id=first_change.id,
        changed_by="operator",
        change_source="api.rollback",
    )

    settings = resolve_runtime_settings(conn)
    assert settings.max_autofix_per_pr == 3


def test_rollback_config_value_rejects_unknown_key(
    monkeypatch, tmp_path
) -> None:
    _clear_runtime_override_env(monkeypatch, tmp_path)
    conn = _make_conn()

    try:
        rollback_config_value(
            conn,
            key="runtime.nonexistent",
            target_audit_id=1,
        )
    except ValueError as exc:
        assert "unknown runtime setting" in str(exc)
    else:
        raise AssertionError("expected ValueError for unknown key")


def test_rollback_config_value_rejects_env_only_key(
    monkeypatch, tmp_path
) -> None:
    _clear_runtime_override_env(monkeypatch, tmp_path)
    conn = _make_conn()

    try:
        rollback_config_value(
            conn,
            key=RUNTIME_DB_PATH_KEY,
            target_audit_id=1,
        )
    except ValueError as exc:
        assert "env_only" in str(exc)
    else:
        raise AssertionError("expected ValueError for env_only key")


def test_rollback_config_value_rejects_missing_audit_entry(
    monkeypatch, tmp_path
) -> None:
    _clear_runtime_override_env(monkeypatch, tmp_path)
    conn = _make_conn()

    try:
        rollback_config_value(
            conn,
            key=RUNTIME_MAX_RETRY_ATTEMPTS_KEY,
            target_audit_id=999,
        )
    except ValueError as exc:
        assert "not found" in str(exc)
    else:
        raise AssertionError("expected ValueError for missing audit entry")


def test_audit_entries_have_complete_metadata(monkeypatch, tmp_path) -> None:
    _clear_runtime_override_env(monkeypatch, tmp_path)
    conn = _make_conn()

    save_runtime_setting_values(
        conn,
        {RUNTIME_MAX_RETRY_ATTEMPTS_KEY: "5"},
        changed_by="alice",
        change_source="web.settings",
    )

    entries = get_config_audit_history(conn)

    assert len(entries) == 1
    entry = entries[0]
    assert entry.key == RUNTIME_MAX_RETRY_ATTEMPTS_KEY
    assert entry.old_value is None
    assert entry.new_value == "5"
    assert entry.changed_by == "alice"
    assert entry.change_source == "web.settings"
    assert entry.created_at is not None
    assert entry.id > 0


def test_rollback_list_value_no_spurious_audit(monkeypatch, tmp_path) -> None:
    _clear_runtime_override_env(monkeypatch, tmp_path)
    conn = _make_conn()

    save_runtime_setting_values(
        conn,
        {RUNTIME_BOT_LOGINS_KEY: '["a","b"]'},
        changed_by="test",
        change_source="test",
    )
    save_runtime_setting_values(
        conn,
        {RUNTIME_BOT_LOGINS_KEY: '["c"]'},
        changed_by="test",
        change_source="test",
    )

    entries = get_config_audit_history(conn, key=RUNTIME_BOT_LOGINS_KEY)
    second_change = entries[0]
    assert second_change.new_value == '["c"]'

    rollback_config_value(
        conn,
        key=RUNTIME_BOT_LOGINS_KEY,
        target_audit_id=second_change.id,
        changed_by="operator",
        change_source="api.rollback",
    )

    audit_entries = get_config_audit_history(conn, key=RUNTIME_BOT_LOGINS_KEY)
    assert len(audit_entries) == 3
    rollback_entry = audit_entries[0]
    assert rollback_entry.change_source == "api.rollback"
    assert rollback_entry.new_value == '["a", "b"]'


def test_rollback_same_value_no_audit_entry(monkeypatch, tmp_path) -> None:
    _clear_runtime_override_env(monkeypatch, tmp_path)
    conn = _make_conn()

    save_runtime_setting_values(
        conn,
        {RUNTIME_MAX_RETRY_ATTEMPTS_KEY: "5"},
        changed_by="test",
        change_source="test",
    )
    save_runtime_setting_values(
        conn,
        {RUNTIME_MAX_RETRY_ATTEMPTS_KEY: "8"},
        changed_by="test",
        change_source="test",
    )
    save_runtime_setting_values(
        conn,
        {RUNTIME_MAX_RETRY_ATTEMPTS_KEY: "5"},
        changed_by="test",
        change_source="test",
    )

    entries = get_config_audit_history(conn, key=RUNTIME_MAX_RETRY_ATTEMPTS_KEY)
    assert len(entries) == 3

    second_change = [e for e in entries if e.new_value == "8"][0]

    rollback_config_value(
        conn,
        key=RUNTIME_MAX_RETRY_ATTEMPTS_KEY,
        target_audit_id=second_change.id,
        changed_by="operator",
        change_source="api.rollback",
    )

    audit_entries = get_config_audit_history(conn, key=RUNTIME_MAX_RETRY_ATTEMPTS_KEY)
    assert len(audit_entries) == 3


def test_rollback_uses_changed_by_from_caller(monkeypatch, tmp_path) -> None:
    _clear_runtime_override_env(monkeypatch, tmp_path)
    conn = _make_conn()

    save_runtime_setting_values(
        conn,
        {RUNTIME_MAX_RETRY_ATTEMPTS_KEY: "5"},
        changed_by="alice",
        change_source="web.settings",
    )
    save_runtime_setting_values(
        conn,
        {RUNTIME_MAX_RETRY_ATTEMPTS_KEY: "8"},
        changed_by="bob",
        change_source="web.settings",
    )

    entries = get_config_audit_history(conn, key=RUNTIME_MAX_RETRY_ATTEMPTS_KEY)
    second_change = entries[0]

    rollback_config_value(
        conn,
        key=RUNTIME_MAX_RETRY_ATTEMPTS_KEY,
        target_audit_id=second_change.id,
        changed_by="carol",
        change_source="api.rollback",
    )

    audit_entries = get_config_audit_history(conn, key=RUNTIME_MAX_RETRY_ATTEMPTS_KEY)
    assert audit_entries[0].changed_by == "carol"
