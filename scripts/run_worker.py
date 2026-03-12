from __future__ import annotations

import argparse
import signal
import sys
import time
import traceback
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db import connect_db, init_db
from app.services.agent_runner import run_once
from app.services.queue import claim_next_queued_run, mark_run_finished


_STOP_WORKER = False


def _handle_stop_signal(signum: int, _frame: object) -> None:
    global _STOP_WORKER
    _STOP_WORKER = True
    print(f"received signal={signum}, stopping worker loop")


def _process_one(workspace_dir: str) -> bool:
    with connect_db() as conn:
        run = claim_next_queued_run(conn)
        if run is None:
            return False
        try:
            run_once(conn=conn, run=run, workspace_dir=workspace_dir)
        except Exception as exc:
            run_id = int(run["id"])
            logs_dir = Path(workspace_dir) / "logs"
            logs_dir.mkdir(parents=True, exist_ok=True)
            crash_log = logs_dir / f"autofix-run-{run_id}-worker-crash.log"
            crash_log.write_text(traceback.format_exc(), encoding="utf-8")
            mark_run_finished(
                conn=conn,
                run_id=run_id,
                status="failed",
                error_summary=f"worker_exception: {type(exc).__name__}: {exc}",
                logs_path=str(crash_log),
            )
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
    signal.signal(signal.SIGINT, _handle_stop_signal)
    signal.signal(signal.SIGTERM, _handle_stop_signal)

    if args.once:
        _process_one(workspace_dir=args.workspace_dir)
        return 0

    while not _STOP_WORKER:
        processed = _process_one(workspace_dir=args.workspace_dir)
        if not processed:
            time.sleep(args.interval_seconds)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
