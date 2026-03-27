from __future__ import annotations

import sqlite3

from app.models import SCHEMA_SQL
from app.services.runtime_settings import (
    RUNTIME_BOT_LOGINS_KEY,
    RUNTIME_AUTOFIX_COMMENT_AUTHOR_KEY,
    RUNTIME_GITHUB_WEBHOOK_DEBOUNCE_SECONDS_KEY,
    RUNTIME_MAX_AUTOFIX_PER_PR_KEY,
    RUNTIME_MAX_RETRY_ATTEMPTS_KEY,
    build_runtime_settings_context,
    resolve_runtime_settings,
    save_runtime_settings,
)


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    return conn


def _clear_runtime_override_env(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    for key in (
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


def test_save_runtime_settings_clamps_numeric_values(monkeypatch, tmp_path) -> None:
    _clear_runtime_override_env(monkeypatch, tmp_path)
    conn = _make_conn()

    save_runtime_settings(
        conn,
        github_webhook_debounce_seconds=-5,
        max_autofix_per_pr=-2,
        max_concurrent_runs=0,
        stale_run_timeout_seconds=0,
        pr_lock_ttl_seconds=0,
        max_retry_attempts=0,
        retry_backoff_base_seconds=0,
        retry_backoff_max_seconds=0,
        bot_logins=["ci-helper"],
        noise_comment_patterns=[r"^/retest\b"],
        managed_repo_prefixes=["acme/"],
        autofix_comment_author="autofix-bot",
    )

    runtime_settings = resolve_runtime_settings(conn)

    assert runtime_settings.github_webhook_debounce_seconds == 1
    assert runtime_settings.max_autofix_per_pr == 0
    assert runtime_settings.max_concurrent_runs == 1
    assert runtime_settings.stale_run_timeout_seconds == 1
    assert runtime_settings.pr_lock_ttl_seconds == 1
    assert runtime_settings.max_retry_attempts == 1
    assert runtime_settings.retry_backoff_base_seconds == 1
    assert runtime_settings.retry_backoff_max_seconds == 1


def test_build_runtime_settings_context_formats_list_fields(
    monkeypatch, tmp_path
) -> None:
    _clear_runtime_override_env(monkeypatch, tmp_path)
    conn = _make_conn()

    save_runtime_settings(
        conn,
        github_webhook_debounce_seconds=45,
        max_autofix_per_pr=7,
        max_concurrent_runs=5,
        stale_run_timeout_seconds=321,
        pr_lock_ttl_seconds=654,
        max_retry_attempts=4,
        retry_backoff_base_seconds=12,
        retry_backoff_max_seconds=900,
        bot_logins=["ci-helper", "dependabot[bot]"],
        noise_comment_patterns=[r"^/retest\b", r"^/resolve\b"],
        managed_repo_prefixes=["acme/", "widgets/"],
        autofix_comment_author="autofix-bot",
    )

    context = build_runtime_settings_context(conn)

    assert context["github_webhook_debounce_seconds"] == "45"
    assert context["max_autofix_per_pr"] == "7"
    assert context["bot_logins_text"] == "ci-helper\ndependabot[bot]"
    assert context["noise_comment_patterns_text"] == "^/retest\\b\n^/resolve\\b"
    assert context["managed_repo_prefixes_text"] == "acme/\nwidgets/"
