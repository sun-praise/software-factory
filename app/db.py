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
