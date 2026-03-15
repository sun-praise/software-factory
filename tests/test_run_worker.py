from __future__ import annotations

from pathlib import Path

from scripts import run_worker


class _ConnContext:
    def __enter__(self) -> object:
        return object()

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False


def test_process_one_marks_failed_when_run_once_raises(
    tmp_path: Path, monkeypatch
) -> None:
    calls: dict[str, object] = {}

    monkeypatch.setattr(run_worker, "connect_db", lambda: _ConnContext())
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
