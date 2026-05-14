from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db import connect_db, init_db  # noqa: E402
from app.services.runtime_settings import (  # noqa: E402
    _RUNTIME_SETTING_SPECS,
    _DB_OWNERSHIP,
    load_runtime_setting_rows,
    save_runtime_setting_values,
)


def _collect_env_overrides() -> dict[str, str]:
    import os

    overrides: dict[str, str] = {}
    for spec in _RUNTIME_SETTING_SPECS:
        if spec.ownership != _DB_OWNERSHIP:
            continue
        env_value = os.environ.get(spec.env_var)
        if env_value is None or not env_value.strip():
            continue
        overrides[spec.key] = env_value.strip()
    return overrides


def backfill_runtime_settings(*, dry_run: bool = False) -> int:
    init_db()
    env_overrides = _collect_env_overrides()

    if not env_overrides:
        print("no env-based runtime settings found; nothing to backfill")
        return 0

    with connect_db() as conn:
        stored = load_runtime_setting_rows(conn)
        pending: dict[str, str] = {}
        for key, env_value in env_overrides.items():
            if key in stored:
                print(f"skip  {key}: already in DB ({stored[key]!r})")
                continue
            pending[key] = env_value
            print(f"backfill {key}: {env_value!r}")

        if not pending:
            print("all env-based runtime settings already present in DB")
            return 0

        if dry_run:
            print(f"dry run: would backfill {len(pending)} setting(s)")
            return 0

        save_runtime_setting_values(
            conn,
            pending,
            changed_by="backfill_script",
            change_source="scripts.backfill_runtime_settings",
        )
        print(f"backfilled {len(pending)} setting(s)")
    return 0


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Backfill runtime settings from environment variables into the database"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show what would be backfilled without writing to DB",
    )
    args = parser.parse_args()
    return backfill_runtime_settings(dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
