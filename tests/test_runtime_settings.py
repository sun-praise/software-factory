from __future__ import annotations

import sqlite3

from app.models import SCHEMA_SQL
from app.services.runtime_settings import (
    RUNTIME_BOT_LOGINS_KEY,
    RUNTIME_AUTOFIX_COMMENT_AUTHOR_KEY,
    RUNTIME_DB_PATH_KEY,
    RUNTIME_GITHUB_WEBHOOK_DEBOUNCE_SECONDS_KEY,
    RUNTIME_MAX_AUTOFIX_PER_PR_KEY,
    RUNTIME_MAX_RETRY_ATTEMPTS_KEY,
    build_runtime_settings_context,
    describe_runtime_settings,
    get_runtime_form_int_field_specs,
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


def test_describe_runtime_settings_reports_sources_and_ownership(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.chdir(tmp_path)
    conn = _make_conn()
    conn.execute(
        "INSERT INTO app_feature_flags (key, value) VALUES (?, ?)",
        (RUNTIME_MAX_RETRY_ATTEMPTS_KEY, "7"),
    )
    conn.execute(
        "INSERT INTO app_feature_flags (key, value) VALUES (?, ?)",
        (RUNTIME_MAX_AUTOFIX_PER_PR_KEY, "9"),
    )
    conn.commit()
    monkeypatch.setenv("MAX_AUTOFIX_PER_PR", "4")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "runtime.db"))

    described = describe_runtime_settings(conn)
    described_by_key = {item.key: item for item in described}

    assert described_by_key[RUNTIME_MAX_AUTOFIX_PER_PR_KEY].source == "env"
    assert described_by_key[RUNTIME_MAX_AUTOFIX_PER_PR_KEY].ownership == "db"
    assert described_by_key[RUNTIME_MAX_AUTOFIX_PER_PR_KEY].effective == 4
    assert described_by_key[RUNTIME_MAX_AUTOFIX_PER_PR_KEY].updated_at is None
    assert described_by_key[RUNTIME_MAX_RETRY_ATTEMPTS_KEY].source == "db"
    assert described_by_key[RUNTIME_MAX_RETRY_ATTEMPTS_KEY].updated_at is not None
    assert described_by_key[RUNTIME_DB_PATH_KEY].ownership == "env_only"
    assert described_by_key[RUNTIME_DB_PATH_KEY].source == "env"


def test_describe_runtime_settings_treats_blank_db_path_override_as_default(
    monkeypatch, tmp_path
) -> None:
    _clear_runtime_override_env(monkeypatch, tmp_path)
    (tmp_path / ".env").write_text("DB_PATH=\n", encoding="utf-8")
    conn = _make_conn()

    described = describe_runtime_settings(conn)
    described_by_key = {item.key: item for item in described}

    assert (
        described_by_key[RUNTIME_DB_PATH_KEY].effective == "./data/software_factory.db"
    )
    assert described_by_key[RUNTIME_DB_PATH_KEY].source == "default"


def test_save_runtime_settings_records_audit_rows_only_for_changed_values(
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
        bot_logins=["ci-helper"],
        noise_comment_patterns=[r"^/retest\b"],
        managed_repo_prefixes=["acme/"],
        autofix_comment_author="autofix-bot",
        changed_by="settings_ui",
        change_source="web.settings",
    )
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
        bot_logins=["ci-helper"],
        noise_comment_patterns=[r"^/retest\b"],
        managed_repo_prefixes=["acme/"],
        autofix_comment_author="autofix-bot",
        changed_by="settings_ui",
        change_source="web.settings",
    )

    rows = conn.execute(
        "SELECT key, old_value, new_value, changed_by, change_source FROM app_config_audit_log ORDER BY id"
    ).fetchall()

    assert len(rows) == 12
    assert rows[0]["changed_by"] == "settings_ui"
    assert rows[0]["change_source"] == "web.settings"
    assert rows[0]["old_value"] is None
    assert rows[0]["new_value"] is not None


def test_save_runtime_setting_values_rejects_env_only_keys(
    monkeypatch, tmp_path
) -> None:
    _clear_runtime_override_env(monkeypatch, tmp_path)
    conn = _make_conn()

    try:
        save_runtime_setting_values(
            conn,
            {RUNTIME_DB_PATH_KEY: "/tmp/runtime.db"},
            changed_by="settings_ui",
            change_source="web.settings",
        )
    except ValueError as exc:
        assert "env_only" in str(exc)
    else:
        raise AssertionError("expected ValueError for env_only runtime setting")


def test_save_runtime_setting_values_rejects_invalid_db_values(
    monkeypatch, tmp_path
) -> None:
    _clear_runtime_override_env(monkeypatch, tmp_path)
    conn = _make_conn()

    try:
        save_runtime_setting_values(
            conn,
            {RUNTIME_MAX_RETRY_ATTEMPTS_KEY: "0"},
            changed_by="settings_ui",
            change_source="web.settings",
        )
    except ValueError as exc:
        assert RUNTIME_MAX_RETRY_ATTEMPTS_KEY in str(exc)
    else:
        raise AssertionError("expected ValueError for invalid runtime setting value")


def test_get_runtime_form_int_field_specs_matches_runtime_registry() -> None:
    specs = get_runtime_form_int_field_specs()

    assert specs["github_webhook_debounce_seconds"] == (60, 1)
    assert specs["max_autofix_per_pr"] == (3, 0)
    assert specs["max_retry_attempts"] == (3, 1)
    assert "bot_logins" not in specs
    assert "db_path" not in specs
