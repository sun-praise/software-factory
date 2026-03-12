from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.services.logging_config import (
    archive_log_file,
    cleanup_archived_logs,
    ensure_log_dir,
    get_run_log_path,
)


def test_ensure_log_dir_and_get_run_log_path(tmp_path: Path) -> None:
    logs_dir = ensure_log_dir(tmp_path)
    run_log_path = get_run_log_path(tmp_path, run_id=9)

    assert logs_dir == tmp_path.resolve() / "logs"
    assert logs_dir.exists()
    assert run_log_path == logs_dir / "autofix-run-9.log"


def test_archive_log_file_moves_file_into_archive(tmp_path: Path) -> None:
    log_path = get_run_log_path(tmp_path, run_id=5)
    log_path.write_text("hello\n", encoding="utf-8")

    archived_path = archive_log_file(log_path)

    assert not log_path.exists()
    assert archived_path.exists()
    assert archived_path.parent.name == "archive"
    assert archived_path.read_text(encoding="utf-8") == "hello\n"


def test_cleanup_archived_logs_removes_old_files(tmp_path: Path) -> None:
    archive_dir = ensure_log_dir(tmp_path) / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    old_file = archive_dir / "old.log"
    new_file = archive_dir / "new.log"
    old_file.write_text("old", encoding="utf-8")
    new_file.write_text("new", encoding="utf-8")

    now = datetime(2026, 3, 12, 10, 0, tzinfo=timezone.utc)
    old_timestamp = (now - timedelta(days=10)).timestamp()
    new_timestamp = (now - timedelta(days=1)).timestamp()
    old_file.touch()
    new_file.touch()
    import os

    os.utime(old_file, (old_timestamp, old_timestamp))
    os.utime(new_file, (new_timestamp, new_timestamp))

    removed = cleanup_archived_logs(
        ensure_log_dir(tmp_path),
        older_than_days=7,
        now=now,
    )

    assert removed == [old_file]
    assert not old_file.exists()
    assert new_file.exists()
