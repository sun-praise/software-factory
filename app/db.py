from __future__ import annotations

import sqlite3
from pathlib import Path

from app.config import get_settings
from app.models import SCHEMA_SQL


DEFAULT_DB_PATH = Path("data/software_factory.db")


def get_db_path() -> Path:
    configured_path = get_settings().db_path.strip()
    if configured_path:
        return Path(configured_path).expanduser()
    return DEFAULT_DB_PATH


def connect_db() -> sqlite3.Connection:
    db_path = get_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def init_db() -> None:
    with connect_db() as conn:
        conn.executescript(SCHEMA_SQL)
        _migrate_m6_columns(conn)
        _migrate_app_feature_flags(conn)


ALLOWED_TABLES = {"pull_requests", "autofix_runs"}


def _migrate_m6_columns(conn: sqlite3.Connection) -> None:
    _ensure_columns(
        conn,
        "pull_requests",
        {
            "lock_owner": "TEXT",
            "lock_run_id": "INTEGER",
            "lock_acquired_at": "TEXT",
            "lock_expires_at": "TEXT",
        },
    )
    _ensure_columns(
        conn,
        "autofix_runs",
        {
            "idempotency_key": "TEXT",
            "worker_id": "TEXT",
            "claimed_at": "TEXT",
            "started_at": "TEXT",
            "attempt_count": "INTEGER NOT NULL DEFAULT 0",
            "max_attempts": "INTEGER NOT NULL DEFAULT 3",
            "retryable": "INTEGER NOT NULL DEFAULT 1",
            "retry_after": "TEXT",
            "last_error_code": "TEXT",
            "last_error_at": "TEXT",
            "updated_at": "TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP",
        },
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pull_requests_lock_owner ON pull_requests(lock_owner);"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_autofix_runs_status_retry_after ON autofix_runs(status, retry_after);"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_autofix_runs_idempotency_key ON autofix_runs(idempotency_key);"
    )
    conn.commit()


def _migrate_app_feature_flags(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS app_feature_flags (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.commit()


def _ensure_columns(
    conn: sqlite3.Connection,
    table_name: str,
    expected_columns: dict[str, str],
) -> None:
    """
    Ensure table has expected columns, adding missing ones.

    Security: This function uses SQL string formatting, so we enforce:
    1. table_name must be in ALLOWED_TABLES whitelist
    2. column_name must be alphanumeric (with underscores only)
    3. column_sql is trusted as it comes from hardcoded migration data
    """
    if table_name not in ALLOWED_TABLES:
        raise ValueError(f"Invalid table name: {table_name}")
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()  # nosec B608: table_name validated against ALLOWED_TABLES whitelist above
    existing = {str(row[1]) for row in rows}
    for column_name, column_sql in expected_columns.items():
        if column_name in existing:
            continue
        if not column_name.replace("_", "").isalnum():
            raise ValueError(f"Invalid column name: {column_name}")
        conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_sql}")  # nosec B608: table_name and column_name validated above
