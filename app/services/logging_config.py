from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import shutil


def ensure_log_dir(base_dir: str | Path, relative_dir: str = "logs") -> Path:
    root = Path(base_dir).expanduser().resolve()
    logs_dir = root / relative_dir
    logs_dir.mkdir(parents=True, exist_ok=True)
    return logs_dir


def get_run_log_path(
    base_dir: str | Path,
    run_id: int,
    relative_dir: str = "logs",
    prefix: str = "autofix-run",
) -> Path:
    logs_dir = ensure_log_dir(base_dir=base_dir, relative_dir=relative_dir)
    return logs_dir / f"{prefix}-{run_id}.log"


def archive_log_file(
    log_path: str | Path,
    archive_subdir: str = "archive",
) -> Path:
    source = Path(log_path).expanduser().resolve()
    if not source.exists():
        raise FileNotFoundError(source)

    archive_dir = source.parent / archive_subdir
    archive_dir.mkdir(parents=True, exist_ok=True)
    destination = archive_dir / source.name
    if destination.exists():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        destination = archive_dir / f"{source.stem}-{stamp}{source.suffix}"

    return Path(shutil.move(str(source), str(destination)))


def cleanup_archived_logs(
    base_dir: str | Path,
    archive_subdir: str = "archive",
    older_than_days: int = 7,
    now: datetime | None = None,
) -> list[Path]:
    if older_than_days < 0:
        raise ValueError("older_than_days must be non-negative")

    current_time = _normalize_now(now)
    archive_dir = Path(base_dir).expanduser().resolve() / archive_subdir
    if not archive_dir.exists():
        return []

    cutoff = current_time - timedelta(days=older_than_days)
    removed: list[Path] = []
    for path in archive_dir.iterdir():
        if not path.is_file():
            continue
        modified = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        if modified <= cutoff:
            path.unlink()
            removed.append(path)
    return removed


def _normalize_now(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)
