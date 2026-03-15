from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import pytest

from app.models import SCHEMA_SQL
from app.services.agent_runner import (
    CHECK_COMMAND_TIMEOUT_SECONDS,
    RunnerOps,
    _default_executor,
    _execute_agent_sdks,
    _run_claude_agent,
    _sanitize_log_text,
    _normalize_agent_modes,
    run_once,
)
from app.services.queue import claim_next_queued_run, enqueue_autofix_run
from app.services import agent_runner


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


def test_run_once_success_writes_logs_and_marks_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    monkeypatch.setattr(
        agent_runner,
        "_execute_agent_sdks",
        lambda **kwargs: (True, None, None, "openhands"),
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
    pr_row = conn.execute(
        "SELECT autofix_count FROM pull_requests WHERE repo = ? AND pr_number = ?",
        ("acme/widgets", 7),
    ).fetchone()
    assert row is not None
    assert row["status"] == "success"
    assert row["logs_path"] == str(logs_path)
    assert pr_row is not None
    assert pr_row["autofix_count"] == 1


def test_run_once_failure_marks_failed_and_records_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    monkeypatch.setattr(
        agent_runner,
        "_execute_agent_sdks",
        lambda **kwargs: (True, None, None, "openhands"),
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


def test_run_once_records_comment_failure_in_db(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _make_conn()
    run = _enqueue_and_claim(conn)

    ops = RunnerOps(
        checkout_branch=lambda *_: (True, "checked out"),
        ensure_head_sha=lambda *_: True,
        commit_and_push=lambda **_: {
            "success": True,
            "commit_sha": "deadbeef",
            "error": None,
        },
        post_pr_comment=lambda *_: (False, "api unavailable"),
    )
    monkeypatch.setattr(
        agent_runner,
        "_execute_agent_sdks",
        lambda **kwargs: (True, None, None, "openhands"),
    )

    result = run_once(
        conn=conn,
        run=run,
        workspace_dir=str(tmp_path),
        executor=lambda *_: {"returncode": 0, "stdout": "ok", "stderr": ""},
        ops=ops,
    )

    assert result["status"] == "success"
    assert result["comment_posted"] is False
    assert "pr_comment_failed" in str(result["error_summary"])

    row = conn.execute(
        "SELECT error_summary FROM autofix_runs WHERE id = ?", (run["id"],)
    ).fetchone()
    assert row is not None
    assert "pr_comment_failed" in str(row["error_summary"])


def test_default_executor_passes_timeout(monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)

        class _Result:
            returncode = 0
            stdout = "ok"
            stderr = ""

        return _Result()

    import subprocess

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = _default_executor("pytest -q", str(tmp_path))

    assert captured["timeout"] == CHECK_COMMAND_TIMEOUT_SECONDS
    assert captured["cwd"] == str(tmp_path)
    assert result.returncode == 0


def test_run_once_fails_for_unknown_project_type(tmp_path: Path) -> None:
    conn = _make_conn()
    enqueue_autofix_run(
        conn=conn,
        repo="acme/widgets",
        pr_number=8,
        head_sha="abc999",
        normalized_review_json={
            "summary": "unsupported project",
            "project_type": "elixir",
            "must_fix": [],
            "should_fix": [],
        },
    )
    run = claim_next_queued_run(conn)
    assert run is not None

    result = run_once(conn=conn, run=run, workspace_dir=str(tmp_path))
    assert result["status"] == "failed"
    assert "unsupported_project_type" in str(result["error_summary"])
    assert result["comment_posted"] is False


def test_default_executor_falls_back_to_cli_when_python_module_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    class _Result:
        def __init__(self, returncode: int, stdout: str, stderr: str) -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def fake_run(command, **kwargs):
        calls.append(list(command))
        if command[:3] == [sys.executable, "-m", "ruff"]:
            return _Result(1, "", f"{sys.executable}: No module named ruff")
        if command[:1] == ["ruff"]:
            return _Result(0, "lint ok", "")
        return _Result(0, "ok", "")

    import subprocess

    monkeypatch.setattr(shutil, "which", lambda value: None if value == "python" else f"/usr/bin/{value}")
    monkeypatch.setattr(subprocess, "run", fake_run)

    result = _default_executor("python -m ruff check .", str(tmp_path))

    assert calls == [
        [sys.executable, "-m", "ruff", "check", "."],
        ["ruff", "check", "."],
    ]
    assert result.returncode == 0
    assert result.stdout == "lint ok"


def test_run_once_schedules_retry_for_git_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _make_conn()
    run = _enqueue_and_claim(conn)

    ops = RunnerOps(
        checkout_branch=lambda *_: (True, "checked out"),
        ensure_head_sha=lambda *_: True,
        commit_and_push=lambda **_: {
            "success": False,
            "commit_sha": None,
            "error": "push_rejected",
        },
        post_pr_comment=lambda *_: (True, "ok"),
    )
    monkeypatch.setattr(
        agent_runner,
        "_execute_agent_sdks",
        lambda **kwargs: (True, None, None, "openhands"),
    )

    result = run_once(
        conn=conn,
        run=run,
        workspace_dir=str(tmp_path),
        executor=lambda *_: {"returncode": 0, "stdout": "ok", "stderr": ""},
        ops=ops,
    )

    row = conn.execute(
        "SELECT status, retry_after, logs_path FROM autofix_runs WHERE id = ?",
        (run["id"],),
    ).fetchone()
    assert result["status"] == "retry_scheduled"
    assert row is not None
    assert row["status"] == "retry_scheduled"
    assert row["retry_after"] is not None
    assert row["logs_path"] == str(Path(result["logs_path"]))


def test_run_once_fails_when_agent_sdk_not_configured(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _make_conn()
    run = _enqueue_and_claim(conn)

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
    monkeypatch.setattr(
        agent_runner,
        "_execute_agent_sdks",
        lambda **kwargs: (
            False,
            "ai_not_configured",
            "missing API key",
            None,
        ),
    )

    result = run_once(
        conn=conn,
        run=run,
        workspace_dir=str(tmp_path),
        executor=lambda *_: {"returncode": 0, "stdout": "ok", "stderr": ""},
        ops=ops,
    )

    assert result["status"] == "failed"
    assert "ai_not_configured" in str(result["error_summary"])


def test_run_once_marks_agent_error_as_non_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _make_conn()
    run = _enqueue_and_claim(conn)

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
    monkeypatch.setattr(
        agent_runner,
        "_execute_agent_sdks",
        lambda **kwargs: (
            False,
            "ai_request_client_error",
            "bad request",
            None,
        ),
    )

    result = run_once(
        conn=conn,
        run=run,
        workspace_dir=str(tmp_path),
        executor=lambda *_: {"returncode": 0, "stdout": "ok", "stderr": ""},
        ops=ops,
    )

    assert result["status"] == "failed"
    row = conn.execute(
        "SELECT status, last_error_code, error_summary FROM autofix_runs WHERE id = ?",
        (run["id"],),
    ).fetchone()
    assert row is not None
    assert row["status"] == "failed"
    assert row["last_error_code"] == "ai_request_client_error"
    assert "ai_request_client_error" in str(result["error_summary"])


def test_run_once_schedules_retry_for_retryable_agent_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _make_conn()
    run = _enqueue_and_claim(conn)

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
    monkeypatch.setattr(
        agent_runner,
        "_execute_agent_sdks",
        lambda **kwargs: (
            False,
            "ai_request_failed",
            "temporary failure",
            None,
        ),
    )

    result = run_once(
        conn=conn,
        run=run,
        workspace_dir=str(tmp_path),
        executor=lambda *_: {"returncode": 0, "stdout": "ok", "stderr": ""},
        ops=ops,
    )

    assert result["status"] == "retry_scheduled"
    row = conn.execute(
        "SELECT status, last_error_code, retry_after FROM autofix_runs WHERE id = ?",
        (run["id"],),
    ).fetchone()
    assert row is not None
    assert row["status"] == "retry_scheduled"
    assert row["last_error_code"] == "ai_request_failed"


def test_normalize_agent_modes() -> None:
    assert _normalize_agent_modes(("legacy", "OPENHANDS", "legacy", "")) == (
        "claude_agent_sdk",
        "openhands",
    )
    assert _normalize_agent_modes(("unknown", "", "other")) == (
        "claude_agent_sdk",
        "openhands",
    )


def test_execute_agent_sdks_falls_back_to_claude(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fake_openhands(
        workspace: str,
        run_id: int,
        repo: str,
        pr_number: int,
        prompt: str,
        *,
        command: str,
        timeout_seconds: int,
        on_log_line: object | None = None,
        should_cancel: object | None = None,
    ) -> tuple[bool, str, str | None]:
        calls.append(workspace)
        return False, "openhands failed", "agent_openhands_failed"

    def fake_claude(
        workspace: str,
        run_id: int,
        repo: str,
        pr_number: int,
        prompt: str,
        *,
        command: str,
        timeout_seconds: int,
        on_log_line: object | None = None,
        should_cancel: object | None = None,
    ) -> tuple[bool, str, str | None]:
        calls.append(workspace)
        return True, "claude succeeded", None

    monkeypatch.setattr(agent_runner, "_run_openhands_agent", fake_openhands)
    monkeypatch.setattr(agent_runner, "_run_claude_agent", fake_claude)

    ok, err_code, err_message, selected_mode = agent_runner._execute_agent_sdks(
        workspace="/tmp",
        run_id=123,
        repo="owner/repo",
        pr_number=1,
        prompt="fix this",
        modes=("openhands", "claude_agent_sdk"),
        openhands_command="openhands",
        openhands_command_timeout_seconds=600,
        claude_agent_command="claude",
        claude_agent_command_timeout_seconds=600,
    )
    assert ok is True
    assert err_code is None
    assert err_message is None
    assert selected_mode == "claude_agent_sdk"
    assert calls == ["/tmp", "/tmp"]


def test_run_claude_agent_uses_normalized_command_and_filtered_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}

    class _FakeProcess:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self.returncode = 0
            self.pid = 999
            captured["command"] = list(args[0]) if args else []
            captured.update(kwargs)
            captured["pid"] = self.pid

        def communicate(
            self,
            input: str | None = None,
            timeout: int | float | None = None,
        ) -> tuple[str, str]:
            captured["input"] = input
            captured["timeout"] = timeout
            return "done", ""

        def poll(self) -> int | None:
            return self.returncode

        def __repr__(self) -> str:  # pragma: no cover - debug helper only
            return f"<_FakeProcess returncode={self.returncode}>"

    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    monkeypatch.setenv("UNRELATED_SECRET", "should-not-leak")
    monkeypatch.setenv("PATH", os.environ.get("PATH", ""))
    monkeypatch.setattr(agent_runner.shutil, "which", lambda value: f"/usr/bin/{value}")
    monkeypatch.setattr(agent_runner.subprocess, "Popen", _FakeProcess)

    ok, message, error_code = _run_claude_agent(
        workspace=str(tmp_path),
        run_id=9,
        repo="acme/widgets",
        pr_number=7,
        prompt="fix this",
        command="  claude --print  ",
        timeout_seconds=42,
    )

    assert ok is True
    assert message == "done"
    assert error_code is None
    assert captured["command"] == [
        "claude",
        "--print",
        "--verbose",
        "--output-format",
        "stream-json",
    ]
    assert captured["cwd"] == str(tmp_path)
    assert captured["stdout"] == agent_runner.subprocess.PIPE
    assert captured["stderr"] == agent_runner.subprocess.PIPE
    assert captured["stdin"] == agent_runner.subprocess.PIPE
    assert captured["text"] is True
    assert captured["timeout"] == 42
    assert captured["input"] == "fix this"
    env = captured["env"]
    assert isinstance(env, dict)
    assert env["OPENAI_API_KEY"] == "test-openai-key"
    assert env["SOFTWARE_FACTORY_REPO"] == "acme/widgets"
    assert env["SOFTWARE_FACTORY_PR_NUMBER"] == "7"
    assert env["SOFTWARE_FACTORY_RUN_ID"] == "9"
    assert "UNRELATED_SECRET" not in env


def test_run_claude_agent_rejects_shell_control_tokens(tmp_path: Path) -> None:
    ok, message, error_code = _run_claude_agent(
        workspace=str(tmp_path),
        run_id=9,
        repo="acme/widgets",
        pr_number=7,
        prompt="fix this",
        command="claude && whoami",
        timeout_seconds=42,
    )

    assert ok is False
    assert error_code == "agent_claude_failed"
    assert "unsupported shell control operators" in message


def test_sanitize_log_text_redacts_tokens() -> None:
    raw = "token=abc123 secret: xyz ghp_abcdefghijklmnopqrstuvwxyz"
    masked = _sanitize_log_text(raw)
    assert "abc123" not in masked
    assert "xyz" not in masked
    assert "ghp_abcdefghijklmnopqrstuvwxyz" not in masked
    assert "[REDACTED]" in masked
