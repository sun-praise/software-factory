from __future__ import annotations

import argparse
import atexit
import signal
import sys
import time
import traceback
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db import connect_db, init_db
from app.config import get_settings
from app.services.agent_runner import (
    cleanup_active_agent_processes,
    run_once,
)
from app.services.queue import claim_next_queued_run, mark_run_finished
from app.services.queue import recover_stale_runs
from app.services.retry import RetryConfig, schedule_retry
from app.services.logging_config import get_run_log_path


_STOP_WORKER = False


def _handle_stop_signal(signum: int, _frame: object) -> None:
    global _STOP_WORKER
    _STOP_WORKER = True
    cleanup_active_agent_processes()
    print(f"received signal={signum}, stopping worker loop")


def _process_one(workspace_dir: str) -> bool:
    settings = get_settings()
    with connect_db() as conn:
        run = claim_next_queued_run(
            conn,
            worker_id=settings.worker_id,
            max_running_runs=settings.max_concurrent_runs,
        )
        if run is None:
            return False
        try:
            run_once(conn=conn, run=run, workspace_dir=workspace_dir)
        except Exception as exc:
            run_id = int(run["id"])
            crash_log = get_run_log_path(
                workspace_dir,
                run_id,
                relative_dir=settings.log_dir,
                prefix="autofix-run-worker-crash",
            )
            crash_log.write_text(traceback.format_exc(), encoding="utf-8")
            error_summary = f"worker_exception: {type(exc).__name__}: {exc}"
            config = RetryConfig(
                base_delay_seconds=settings.retry_backoff_base_seconds,
                max_delay_seconds=settings.retry_backoff_max_seconds,
            )
            plan = schedule_retry(
                conn,
                run_id,
                error_code="worker_exception",
                error_summary=error_summary,
                config=config,
            )
            if not plan.scheduled:
                mark_run_finished(
                    conn=conn,
                    run_id=run_id,
                    status="failed",
                    error_summary=error_summary,
                    logs_path=str(crash_log),
                    last_error_code="worker_exception",
                )
            else:
                conn.execute(
                    "UPDATE autofix_runs SET logs_path = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (str(crash_log), run_id),
                )
                conn.commit()
    return True


def _recover_stale_runs() -> int:
    settings = get_settings()
    with connect_db() as conn:
        return recover_stale_runs(
            conn,
            stale_after_seconds=settings.stale_run_timeout_seconds,
            worker_id=settings.worker_id,
        )


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
    recovered_count = _recover_stale_runs()
    if recovered_count:
        print(f"recovered stale runs={recovered_count}")
    atexit.register(cleanup_active_agent_processes)
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
