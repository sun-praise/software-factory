from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db import connect_db, init_db
from app.services.agent_runner import run_once
from app.services.queue import claim_next_queued_run


def _process_one(workspace_dir: str) -> bool:
    with connect_db() as conn:
        run = claim_next_queued_run(conn)
        if run is None:
            return False
        run_once(conn=conn, run=run, workspace_dir=workspace_dir)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Run autofix queue worker")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--once", dest="once", action="store_true")
    group.add_argument("--loop", dest="once", action="store_false")
    parser.set_defaults(once=True)
    parser.add_argument("--interval-seconds", type=float, default=2.0)
    parser.add_argument("--workspace-dir", default=str(ROOT))
    args = parser.parse_args()

    init_db()

    if args.once:
        _process_one(workspace_dir=args.workspace_dir)
        return 0

    while True:
        processed = _process_one(workspace_dir=args.workspace_dir)
        if not processed:
            time.sleep(args.interval_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
