from __future__ import annotations

import os
import sqlite3

from app.config import get_settings
from app.models import SCHEMA_SQL
from app.services.runtime_settings import (
    RUNTIME_GITHUB_WEBHOOK_DEBOUNCE_SECONDS_KEY,
    RUNTIME_MAX_AUTOFIX_PER_PR_KEY,
    RUNTIME_MAX_RETRY_ATTEMPTS_KEY,
    load_runtime_setting_rows,
)
from scripts.backfill_runtime_settings import _collect_env_overrides


def _make_conn(tmp_path) -> sqlite3.Connection:
    get_settings.cache_clear()
    db_path = tmp_path / "backfill_test.db"
    os.environ["DB_PATH"] = str(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    return conn


def _clear_runtime_env(monkeypatch) -> None:
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


def test_collect_env_overrides_skips_empty_and_unset(monkeypatch) -> None:
    _clear_runtime_env(monkeypatch)
    monkeypatch.setenv("MAX_AUTOFIX_PER_PR", "5")
    monkeypatch.setenv("MAX_RETRY_ATTEMPTS", "")

    overrides = _collect_env_overrides()

    assert RUNTIME_MAX_AUTOFIX_PER_PR_KEY in overrides
    assert overrides[RUNTIME_MAX_AUTOFIX_PER_PR_KEY] == "5"
    assert RUNTIME_MAX_RETRY_ATTEMPTS_KEY not in overrides


def test_backfill_writes_missing_settings_to_db(monkeypatch, tmp_path) -> None:
    _clear_runtime_env(monkeypatch)
    monkeypatch.setenv("GITHUB_WEBHOOK_DEBOUNCE_SECONDS", "42")
    monkeypatch.setenv("MAX_AUTOFIX_PER_PR", "7")
    conn = _make_conn(tmp_path)

    from scripts.backfill_runtime_settings import backfill_runtime_settings

    backfill_runtime_settings(dry_run=False)

    stored = load_runtime_setting_rows(conn)
    conn.close()

    assert stored.get(RUNTIME_GITHUB_WEBHOOK_DEBOUNCE_SECONDS_KEY) == "42"
    assert stored.get(RUNTIME_MAX_AUTOFIX_PER_PR_KEY) == "7"


def test_backfill_skips_already_stored_keys(monkeypatch, tmp_path) -> None:
    _clear_runtime_env(monkeypatch)
    monkeypatch.setenv("MAX_AUTOFIX_PER_PR", "7")
    conn = _make_conn(tmp_path)
    conn.execute(
        "INSERT INTO app_feature_flags (key, value) VALUES (?, ?)",
        (RUNTIME_MAX_AUTOFIX_PER_PR_KEY, "3"),
    )
    conn.commit()

    from scripts.backfill_runtime_settings import backfill_runtime_settings

    backfill_runtime_settings(dry_run=False)

    stored = load_runtime_setting_rows(conn)
    conn.close()

    assert stored.get(RUNTIME_MAX_AUTOFIX_PER_PR_KEY) == "3"


def test_backfill_dry_run_does_not_write(monkeypatch, tmp_path) -> None:
    _clear_runtime_env(monkeypatch)
    monkeypatch.setenv("MAX_AUTOFIX_PER_PR", "9")
    conn = _make_conn(tmp_path)

    from scripts.backfill_runtime_settings import backfill_runtime_settings

    backfill_runtime_settings(dry_run=True)

    stored = load_runtime_setting_rows(conn)
    conn.close()

    assert RUNTIME_MAX_AUTOFIX_PER_PR_KEY not in stored


def test_backfill_noop_when_no_env_vars(monkeypatch, tmp_path) -> None:
    _clear_runtime_env(monkeypatch)
    _make_conn(tmp_path)

    from scripts.backfill_runtime_settings import backfill_runtime_settings

    result = backfill_runtime_settings(dry_run=False)
    assert result == 0


def test_backfill_records_audit_rows(monkeypatch, tmp_path) -> None:
    _clear_runtime_env(monkeypatch)
    monkeypatch.setenv("MAX_AUTOFIX_PER_PR", "5")
    conn = _make_conn(tmp_path)

    from scripts.backfill_runtime_settings import backfill_runtime_settings

    backfill_runtime_settings(dry_run=False)

    rows = conn.execute(
        "SELECT changed_by, change_source FROM app_config_audit_log"
    ).fetchall()
    conn.close()

    assert len(rows) >= 1
    assert rows[0]["changed_by"] == "backfill_script"
    assert rows[0]["change_source"] == "scripts.backfill_runtime_settings"
