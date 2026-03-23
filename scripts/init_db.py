from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db import connect_db, get_db_path, init_db  # noqa: E402


def main() -> int:
    init_db()

    with connect_db() as conn:
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        indexes = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND name LIKE 'idx_%' ORDER BY name"
        ).fetchall()

    print(f"Database initialized: {get_db_path()}")
    print(f"Tables ({len(tables)}): {', '.join(row['name'] for row in tables)}")
    print(f"Indexes ({len(indexes)}): {', '.join(row['name'] for row in indexes)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
