from __future__ import annotations

import sqlite3

from app.models import SCHEMA_SQL
from app.services.runtime_settings import (
    RUNTIME_BOT_LOGINS_KEY,
    RUNTIME_AUTOFIX_COMMENT_AUTHOR_KEY,
    RUNTIME_GITHUB_WEBHOOK_DEBOUNCE_SECONDS_KEY,
    RUNTIME_MAX_AUTOFIX_PER_PR_KEY,
    RUNTIME_MAX_RETRY_ATTEMPTS_KEY,
    resolve_runtime_settings,
)


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    return conn


def test_resolve_runtime_settings_prefers_env_over_db(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    conn = _make_conn()
    conn.execute(
        "INSERT INTO app_feature_flags (key, value) VALUES (?, ?)",
        (RUNTIME_MAX_AUTOFIX_PER_PR_KEY, "9"),
    )
    conn.commit()

    monkeypatch.setenv("MAX_AUTOFIX_PER_PR", "4")

    runtime_settings = resolve_runtime_settings(conn)

    assert runtime_settings.max_autofix_per_pr == 4


def test_resolve_runtime_settings_uses_db_values_when_env_not_set(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.chdir(tmp_path)
    conn = _make_conn()
    conn.execute(
        "INSERT INTO app_feature_flags (key, value) VALUES (?, ?)",
        (RUNTIME_GITHUB_WEBHOOK_DEBOUNCE_SECONDS_KEY, "42"),
    )
    conn.execute(
        "INSERT INTO app_feature_flags (key, value) VALUES (?, ?)",
        (RUNTIME_MAX_RETRY_ATTEMPTS_KEY, "6"),
    )
    conn.execute(
        "INSERT INTO app_feature_flags (key, value) VALUES (?, ?)",
        (RUNTIME_BOT_LOGINS_KEY, '["ci-helper", "dependabot[bot]"]'),
    )
    conn.commit()

    monkeypatch.delenv("GITHUB_WEBHOOK_DEBOUNCE_SECONDS", raising=False)
    monkeypatch.delenv("MAX_RETRY_ATTEMPTS", raising=False)
    monkeypatch.delenv("BOT_LOGINS", raising=False)

    runtime_settings = resolve_runtime_settings(conn)

    assert runtime_settings.github_webhook_debounce_seconds == 42
    assert runtime_settings.max_retry_attempts == 6
    assert runtime_settings.bot_logins == ("ci-helper", "dependabot[bot]")


def test_resolve_runtime_settings_falls_back_on_invalid_db_values(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.chdir(tmp_path)
    conn = _make_conn()
    conn.execute(
        "INSERT INTO app_feature_flags (key, value) VALUES (?, ?)",
        (RUNTIME_GITHUB_WEBHOOK_DEBOUNCE_SECONDS_KEY, "0"),
    )
    conn.execute(
        "INSERT INTO app_feature_flags (key, value) VALUES (?, ?)",
        (RUNTIME_BOT_LOGINS_KEY, "[invalid json"),
    )
    conn.commit()

    monkeypatch.delenv("GITHUB_WEBHOOK_DEBOUNCE_SECONDS", raising=False)
    monkeypatch.delenv("BOT_LOGINS", raising=False)

    runtime_settings = resolve_runtime_settings(conn)

    assert runtime_settings.github_webhook_debounce_seconds == 60
    assert runtime_settings.bot_logins == ()


def test_resolve_runtime_settings_reads_dotenv_overrides(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("MAX_AUTOFIX_PER_PR=5\n", encoding="utf-8")
    conn = _make_conn()
    conn.execute(
        "INSERT INTO app_feature_flags (key, value) VALUES (?, ?)",
        (RUNTIME_MAX_AUTOFIX_PER_PR_KEY, "9"),
    )
    conn.commit()

    monkeypatch.delenv("MAX_AUTOFIX_PER_PR", raising=False)

    runtime_settings = resolve_runtime_settings(conn)

    assert runtime_settings.max_autofix_per_pr == 5


def test_resolve_runtime_settings_allows_blank_autofix_author(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.chdir(tmp_path)
    conn = _make_conn()
    conn.execute(
        "INSERT INTO app_feature_flags (key, value) VALUES (?, ?)",
        (RUNTIME_AUTOFIX_COMMENT_AUTHOR_KEY, ""),
    )
    conn.commit()

    monkeypatch.delenv("AUTOFIX_COMMENT_AUTHOR", raising=False)

    runtime_settings = resolve_runtime_settings(conn)

    assert runtime_settings.autofix_comment_author == ""
