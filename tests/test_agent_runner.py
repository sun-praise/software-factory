from __future__ import annotations

import sqlite3
from pathlib import Path

from app.models import SCHEMA_SQL
from app.services.agent_runner import RunnerOps, run_once
from app.services.queue import claim_next_queued_run, enqueue_autofix_run


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    return conn


def _enqueue_and_claim(conn: sqlite3.Connection) -> dict:
    enqueue_autofix_run(
        conn=conn,
        repo="acme/widgets",
        pr_number=7,
        head_sha="abc123",
        normalized_review_json={
            "summary": "1 blocking issue",
            "project_type": "python",
            "branch": "feature/test",
            "must_fix": [{"path": "app/main.py", "line": 12, "text": "Fix bug"}],
            "should_fix": [],
        },
    )
    run = claim_next_queued_run(conn)
    assert run is not None
    return run


def test_run_once_success_writes_logs_and_marks_success(tmp_path: Path) -> None:
    conn = _make_conn()
    run = _enqueue_and_claim(conn)

    def executor(command: str, workspace_dir: str) -> dict[str, object]:
        return {
            "returncode": 0,
            "stdout": f"ok: {command} @ {workspace_dir}",
            "stderr": "",
        }

    ops = RunnerOps(
        checkout_branch=lambda *_: (True, "checked out"),
        ensure_head_sha=lambda *_: True,
        commit_and_push=lambda **_: {
            "success": True,
            "commit_sha": "deadbeef",
            "error": None,
        },
        post_pr_comment=lambda *_: (True, "ok"),
    )

    result = run_once(
        conn=conn,
        run=run,
        workspace_dir=str(tmp_path),
        executor=executor,
        ops=ops,
    )

    assert result["status"] == "success"
    assert result["error_summary"] is None
    assert result["commit_sha"] == "deadbeef"

    logs_path = Path(result["logs_path"])
    assert logs_path.exists()
    logs_text = logs_path.read_text(encoding="utf-8")
    assert "pytest -q" in logs_text
    assert "ruff check ." in logs_text
    assert "mypy ." in logs_text

    row = conn.execute(
        "SELECT * FROM autofix_runs WHERE id = ?", (run["id"],)
    ).fetchone()
    assert row is not None
    assert row["status"] == "success"
    assert row["logs_path"] == str(logs_path)


def test_run_once_failure_marks_failed_and_records_error(tmp_path: Path) -> None:
    conn = _make_conn()
    run = _enqueue_and_claim(conn)

    def executor(command: str, workspace_dir: str) -> dict[str, object]:
        if "ruff check" in command:
            return {"returncode": 2, "stdout": "", "stderr": "lint failed"}
        return {"returncode": 0, "stdout": "ok", "stderr": ""}

    ops = RunnerOps(
        checkout_branch=lambda *_: (True, "checked out"),
        ensure_head_sha=lambda *_: True,
        commit_and_push=lambda **_: {
            "success": True,
            "commit_sha": "deadbeef",
            "error": None,
        },
        post_pr_comment=lambda *_: (True, "ok"),
    )

    result = run_once(
        conn=conn,
        run=run,
        workspace_dir=str(tmp_path),
        executor=executor,
        ops=ops,
    )

    assert result["status"] == "failed"
    assert "ruff check" in str(result["error_summary"])

    logs_path = Path(result["logs_path"])
    assert logs_path.exists()
    logs_text = logs_path.read_text(encoding="utf-8")
    assert "lint failed" in logs_text

    row = conn.execute(
        "SELECT * FROM autofix_runs WHERE id = ?", (run["id"],)
    ).fetchone()
    assert row is not None
    assert row["status"] == "failed"
    assert "ruff check" in str(row["error_summary"])
