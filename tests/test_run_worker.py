from __future__ import annotations

from pathlib import Path
from typing import Literal

from scripts import run_worker


class _ConnContext:
    def __enter__(self) -> object:
        return object()

    def __exit__(self, exc_type, exc, tb) -> Literal[False]:
        return False


def test_process_one_marks_failed_when_run_once_raises(
    tmp_path: Path, monkeypatch
) -> None:
    calls: dict[str, object] = {}

    monkeypatch.setattr(run_worker, "connect_db", lambda: _ConnContext())
    monkeypatch.setattr(
        run_worker,
        "resolve_runtime_settings",
        lambda conn: type(
            "RuntimeSettings",
            (),
            {
                "max_concurrent_runs": 3,
                "retry_backoff_base_seconds": 30,
                "retry_backoff_max_seconds": 1800,
            },
        )(),
    )
    monkeypatch.setattr(
        run_worker,
        "claim_next_queued_run",
        lambda conn, **kwargs: {"id": 9},
    )

    def _boom(**kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(run_worker, "run_once", _boom)
    monkeypatch.setattr(
        run_worker,
        "schedule_retry",
        lambda *args, **kwargs: type("Plan", (), {"scheduled": False})(),
    )

    def _mark_run_finished(**kwargs):
        calls.update(kwargs)

    monkeypatch.setattr(run_worker, "mark_run_finished", _mark_run_finished)

    processed = run_worker._process_one(str(tmp_path))

    assert processed is True
    assert calls["run_id"] == 9
    assert calls["status"] == "failed"
    assert "worker_exception" in str(calls["error_summary"])
    logs_path = Path(str(calls["logs_path"]))
    assert logs_path.exists()


def test_stop_signal_sets_loop_flag() -> None:
    run_worker._STOP_WORKER = False
    run_worker._handle_stop_signal(15, None)
    assert run_worker._STOP_WORKER is True


def test_stop_signal_invokes_agent_cleanup(monkeypatch) -> None:
    run_worker._STOP_WORKER = False
    calls: dict[str, int] = {"count": 0}

    def _fake_cleanup() -> None:
        calls["count"] += 1

    monkeypatch.setattr(run_worker, "cleanup_active_agent_processes", _fake_cleanup)
    run_worker._handle_stop_signal(15, None)

    assert run_worker._STOP_WORKER is True
    assert calls["count"] == 1


def test_recover_stale_runs_uses_worker_settings(monkeypatch) -> None:
    calls: dict[str, object] = {}

    class _ConnContext:
        def __enter__(self) -> object:
            return object()

        def __exit__(self, exc_type, exc, tb) -> Literal[False]:
            return False

    class _Settings:
        worker_id = "worker-a"

    class _RuntimeSettings:
        stale_run_timeout_seconds = 123

    monkeypatch.setattr(run_worker, "connect_db", lambda: _ConnContext())
    monkeypatch.setattr(run_worker, "get_settings", lambda: _Settings())
    monkeypatch.setattr(
        run_worker,
        "resolve_runtime_settings",
        lambda conn: _RuntimeSettings(),
    )

    def _recover(conn, *, stale_after_seconds, worker_id):
        calls["stale_after_seconds"] = stale_after_seconds
        calls["worker_id"] = worker_id
        return 2

    monkeypatch.setattr(run_worker, "recover_stale_runs", _recover)

    recovered = run_worker._recover_stale_runs()

    assert recovered == 2
    assert calls["stale_after_seconds"] == 123
    assert calls["worker_id"] == "worker-a"
