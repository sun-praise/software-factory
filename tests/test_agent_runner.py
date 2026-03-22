from __future__ import annotations

import os
import shutil
import sqlite3
import threading
from pathlib import Path

import pytest

from app.models import SCHEMA_SQL
from app.services.agent_runner import (
    CHECK_COMMAND_TIMEOUT_SECONDS,
    RunnerOps,
    _build_agent_env,
    _consume_claude_stream,
    _default_executor,
    _build_run_progress_callback,
    _execute_agent_sdks,
    _render_claude_stream_record,
    _run_claude_agent,
    _sanitize_log_text,
    _normalize_agent_modes,
    run_once,
)
from app.services.queue import (
    append_run_operator_hint,
    claim_next_queued_run,
    enqueue_autofix_run,
)
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
            "error_stage": None,
            "remote": "origin",
            "branch": "feature/test",
            "pushed_ref": "origin/feature/test",
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
    assert "git_push: success ref=origin/feature/test commit=deadbeef" in logs_text
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
    executor_calls = {"count": 0}

    def executor(command: str, workspace_dir: str) -> dict[str, object]:
        executor_calls["count"] += 1
        # First execution is the baseline validation pass. Every later call is a
        # post-agent validation attempt, which should keep failing in this test.
        if executor_calls["count"] >= 2:
            return {"returncode": 2, "stdout": "", "stderr": "lint failed"}
        return {"returncode": 0, "stdout": "ok", "stderr": ""}

    ops = RunnerOps(
        checkout_branch=lambda *_: (True, "checked out"),
        ensure_head_sha=lambda *_: True,
        commit_and_push=lambda **_: {
            "success": True,
            "commit_sha": "deadbeef",
            "error": None,
            "error_stage": None,
            "remote": "origin",
            "branch": "feature/test",
            "pushed_ref": "origin/feature/test",
        },
        post_pr_comment=lambda *_: (True, "ok"),
        collect_check_commands=lambda *_: ["python -m ruff check ."],
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


def test_run_once_fails_fast_for_manual_issue_without_context(tmp_path: Path) -> None:
    conn = _make_conn()
    enqueue_autofix_run(
        conn=conn,
        repo="acme/widgets",
        pr_number=7,
        head_sha="abc123",
        normalized_review_json={
            "summary": "1 blocking issue",
            "project_type": "python",
            "must_fix": [
                {
                    "source": "manual_issue",
                    "path": None,
                    "line": None,
                    "text": "Manual issue submission: https://github.com/acme/widgets/pull/7",
                    "severity": "P1",
                }
            ],
            "should_fix": [],
        },
    )
    run = claim_next_queued_run(conn)
    assert run is not None

    result = run_once(
        conn=conn,
        run=run,
        workspace_dir=str(tmp_path),
        executor=lambda *_: {"returncode": 0, "stdout": "ok", "stderr": ""},
        ops=RunnerOps(post_pr_comment=lambda *_: (True, "ok")),
    )

    assert result["status"] == "failed"
    assert "manual_issue_context_missing" in str(result["error_summary"])


def test_collect_pull_request_metadata_returns_empty_when_gh_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(*args, **kwargs):
        raise FileNotFoundError("gh")

    monkeypatch.setattr(agent_runner.subprocess, "run", fake_run)

    assert (
        agent_runner._collect_pull_request_metadata(repo="acme/widgets", pr_number=7)
        == {}
    )


def test_collect_pull_request_metadata_returns_empty_on_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(*args, **kwargs):
        raise agent_runner.subprocess.TimeoutExpired(cmd="gh pr view", timeout=30)

    monkeypatch.setattr(agent_runner.subprocess, "run", fake_run)

    assert (
        agent_runner._collect_pull_request_metadata(repo="acme/widgets", pr_number=7)
        == {}
    )


def test_run_once_injects_repo_agents_md_into_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _make_conn()
    run = _enqueue_and_claim(conn)
    prompts: list[str] = []
    (tmp_path / "AGENTS.md").write_text(
        "Do not edit generated files.\nRun pytest before finishing.\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(
        agent_runner,
        "_prepare_run_workspace",
        lambda **kwargs: (str(tmp_path), None, "feature/test", "abc123"),
    )

    def fake_execute_agent_sdks(**kwargs):
        prompts.append(str(kwargs["prompt"]))
        return True, None, None, "claude_agent_sdk"

    monkeypatch.setattr(agent_runner, "_execute_agent_sdks", fake_execute_agent_sdks)

    result = run_once(
        conn=conn,
        run=run,
        workspace_dir=str(tmp_path),
        executor=lambda *_: {"returncode": 0, "stdout": "ok", "stderr": ""},
        ops=RunnerOps(
            commit_and_push=lambda **_: {
                "success": True,
                "commit_sha": "deadbeef",
                "error": None,
                "error_stage": None,
                "remote": "origin",
                "branch": "feature/test",
                "pushed_ref": "origin/feature/test",
            },
            post_pr_comment=lambda *_: (True, "ok"),
        ),
    )

    assert result["status"] == "success"
    assert len(prompts) == 1
    assert "Repository Instructions (AGENTS.md)" in prompts[0]
    assert "Do not edit generated files." in prompts[0]


def test_prepare_run_workspace_skips_pr_refetch_when_branch_and_head_known(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cloned_workspace = tmp_path / "run-workspace"
    cloned_workspace.mkdir()
    checkout_calls: list[tuple[str | None, str | None]] = []

    monkeypatch.setattr(agent_runner, "_ensure_repo_cache", lambda **kwargs: None)
    monkeypatch.setattr(
        agent_runner,
        "_create_run_workspace_clone",
        lambda **kwargs: str(cloned_workspace),
    )

    def fake_checkout_run_workspace_target(**kwargs) -> None:
        checkout_calls.append(
            (kwargs.get("resolved_branch"), kwargs.get("resolved_head_sha"))
        )

    def fail_fetch_pull_request_head(**kwargs):
        raise AssertionError("_fetch_pull_request_head should not be called")

    monkeypatch.setattr(
        agent_runner,
        "_checkout_run_workspace_target",
        fake_checkout_run_workspace_target,
    )
    monkeypatch.setattr(
        agent_runner,
        "_fetch_pull_request_head",
        fail_fetch_pull_request_head,
    )

    workspace_dir, agent_workspace, resolved_branch, resolved_head_sha = (
        agent_runner._prepare_run_workspace(
            runtime_root=str(tmp_path),
            repo="acme/widgets",
            pr_number=7,
            run_id=123,
            branch="feature/test",
            head_sha="abc123",
        )
    )

    assert workspace_dir == str(cloned_workspace)
    assert agent_workspace == str(cloned_workspace)
    assert resolved_branch == "feature/test"
    assert resolved_head_sha == "abc123"
    assert checkout_calls == [("feature/test", "abc123")]


def test_run_once_returns_failed_checks_to_agent_and_retries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _make_conn()
    run = _enqueue_and_claim(conn)
    prompts: list[str] = []
    executor_calls = {"count": 0}

    def executor(command: str, workspace_dir: str) -> dict[str, object]:
        executor_calls["count"] += 1
        # First execution is the baseline validation pass. The second call is the
        # first post-agent validation attempt, which should fail in this test.
        if executor_calls["count"] == 2:
            return {"returncode": 1, "stdout": "", "stderr": "lint failed"}
        return {"returncode": 0, "stdout": "ok", "stderr": ""}

    ops = RunnerOps(
        checkout_branch=lambda *_: (True, "checked out"),
        ensure_head_sha=lambda *_: True,
        commit_and_push=lambda **_: {
            "success": True,
            "commit_sha": "deadbeef",
            "error": None,
            "error_stage": None,
            "remote": "origin",
            "branch": "feature/test",
            "pushed_ref": "origin/feature/test",
        },
        post_pr_comment=lambda *_: (True, "ok"),
        collect_check_commands=lambda *_: ["python -m ruff check ."],
    )

    def fake_execute_agent_sdks(**kwargs):
        prompts.append(str(kwargs["prompt"]))
        return True, None, None, "claude_agent_sdk"

    monkeypatch.setattr(agent_runner, "_execute_agent_sdks", fake_execute_agent_sdks)

    result = run_once(
        conn=conn,
        run=run,
        workspace_dir=str(tmp_path),
        executor=executor,
        ops=ops,
    )

    assert result["status"] == "success"
    assert result["commit_sha"] == "deadbeef"
    assert len(prompts) == 2
    assert "Validation feedback from the previous attempt:" in prompts[1]
    assert "[failed-check] python -m ruff check ." in prompts[1]
    assert "lint failed" in prompts[1]


def test_run_once_rereads_operator_hints_between_attempts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _make_conn()
    run = _enqueue_and_claim(conn)
    prompts: list[str] = []
    executor_calls = {"count": 0}

    def executor(command: str, workspace_dir: str) -> dict[str, object]:
        executor_calls["count"] += 1
        if executor_calls["count"] == 2:
            return {"returncode": 1, "stdout": "", "stderr": "lint failed"}
        return {"returncode": 0, "stdout": "ok", "stderr": ""}

    ops = RunnerOps(
        checkout_branch=lambda *_: (True, "checked out"),
        ensure_head_sha=lambda *_: True,
        commit_and_push=lambda **_: {
            "success": True,
            "commit_sha": "deadbeef",
            "error": None,
            "error_stage": None,
            "remote": "origin",
            "branch": "feature/test",
            "pushed_ref": "origin/feature/test",
        },
        post_pr_comment=lambda *_: (True, "ok"),
        collect_check_commands=lambda *_: ["python -m ruff check ."],
    )

    def fake_execute_agent_sdks(**kwargs):
        prompts.append(str(kwargs["prompt"]))
        if len(prompts) == 1:
            append_run_operator_hint(
                conn,
                int(run["id"]),
                "Only touch app/services/filter.py",
            )
        return True, None, None, "claude_agent_sdk"

    monkeypatch.setattr(agent_runner, "_execute_agent_sdks", fake_execute_agent_sdks)

    result = run_once(
        conn=conn,
        run=run,
        workspace_dir=str(tmp_path),
        executor=executor,
        ops=ops,
    )

    assert result["status"] == "success"
    assert len(prompts) == 2
    assert "Operator Hints:" not in prompts[0]
    assert "Operator Hints:" in prompts[1]
    assert "Only touch app/services/filter.py" in prompts[1]


def test_run_once_allows_push_when_only_preexisting_failures_remain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _make_conn()
    run = _enqueue_and_claim(conn)
    calls = {"count": 0}

    def executor(command: str, workspace_dir: str) -> dict[str, object]:
        calls["count"] += 1
        if command == "python -m mypy .":
            return {
                "returncode": 1,
                "stdout": "app/main.py:1: error: preexisting",
                "stderr": "",
            }
        return {"returncode": 0, "stdout": "ok", "stderr": ""}

    ops = RunnerOps(
        checkout_branch=lambda *_: (True, "checked out"),
        ensure_head_sha=lambda *_: True,
        commit_and_push=lambda **_: {
            "success": True,
            "commit_sha": "deadbeef",
            "error": None,
            "error_stage": None,
            "remote": "origin",
            "branch": "feature/test",
            "pushed_ref": "origin/feature/test",
        },
        post_pr_comment=lambda *_: (True, "ok"),
        collect_check_commands=lambda *_: ["python -m mypy ."],
    )

    monkeypatch.setattr(
        agent_runner,
        "_execute_agent_sdks",
        lambda **kwargs: (True, None, None, "claude_agent_sdk"),
    )

    result = run_once(
        conn=conn,
        run=run,
        workspace_dir=str(tmp_path),
        executor=executor,
        ops=ops,
    )

    assert result["status"] == "success"
    assert result["commit_sha"] == "deadbeef"
    assert "preexisting_checks_failed" in str(result["error_summary"])
    assert calls["count"] == 2


def test_run_once_fails_when_new_check_failures_are_introduced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _make_conn()
    run = _enqueue_and_claim(conn)
    calls = {"count": 0}

    def executor(command: str, workspace_dir: str) -> dict[str, object]:
        calls["count"] += 1
        if calls["count"] == 1:
            return {
                "returncode": 1,
                "stdout": "app/main.py:1: error: preexisting",
                "stderr": "",
            }
        return {
            "returncode": 1,
            "stdout": "app/main.py:1: error: preexisting\napp/new.py:2: error: introduced",
            "stderr": "",
        }

    prompts: list[str] = []
    ops = RunnerOps(
        checkout_branch=lambda *_: (True, "checked out"),
        ensure_head_sha=lambda *_: True,
        commit_and_push=lambda **_: {
            "success": True,
            "commit_sha": "deadbeef",
            "error": None,
            "error_stage": None,
            "remote": "origin",
            "branch": "feature/test",
            "pushed_ref": "origin/feature/test",
        },
        post_pr_comment=lambda *_: (True, "ok"),
        collect_check_commands=lambda *_: ["python -m mypy ."],
    )

    def fake_execute_agent_sdks(**kwargs):
        prompts.append(str(kwargs["prompt"]))
        return True, None, None, "claude_agent_sdk"

    monkeypatch.setattr(agent_runner, "_execute_agent_sdks", fake_execute_agent_sdks)

    result = run_once(
        conn=conn,
        run=run,
        workspace_dir=str(tmp_path),
        executor=executor,
        ops=ops,
    )

    assert result["status"] == "failed"
    assert len(prompts) == 3
    assert "introduced" in prompts[1]


def test_run_once_recovers_from_bootstrap_failures_before_checks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _make_conn()
    run = _enqueue_and_claim(conn)
    prompts: list[str] = []
    bootstrap_calls = {"count": 0}

    ops = RunnerOps(
        checkout_branch=lambda *_: (True, "checked out"),
        ensure_head_sha=lambda *_: True,
        commit_and_push=lambda **_: {
            "success": True,
            "commit_sha": "deadbeef",
            "error": None,
        },
        post_pr_comment=lambda *_: (True, "ok"),
        collect_check_commands=lambda *_: ["python -m ruff check ."],
    )

    def fake_execute_agent_sdks(**kwargs):
        prompts.append(str(kwargs["prompt"]))
        return True, None, None, "claude_agent_sdk"

    def fake_bootstrap(_workspace_dir: str, *, commands: list[str]):
        assert commands == ["python -m ruff check ."]
        bootstrap_calls["count"] += 1
        if bootstrap_calls["count"] == 1:
            return agent_runner.WorkspaceBootstrapResult(
                ok=False,
                skipped=False,
                kind="python",
                details=(
                    {
                        "command": ".venv/bin/python -m pip install -r requirements.txt",
                        "exit_code": 1,
                        "stdout": "",
                        "stderr": "No module named pip",
                    },
                ),
                error_summary="workspace_bootstrap_failed: python",
            )
        return agent_runner.WorkspaceBootstrapResult(
            ok=True,
            skipped=True,
            kind="python",
        )

    monkeypatch.setattr(agent_runner, "_execute_agent_sdks", fake_execute_agent_sdks)
    monkeypatch.setattr(agent_runner, "_bootstrap_workspace_runtime", fake_bootstrap)

    result = run_once(
        conn=conn,
        run=run,
        workspace_dir=str(tmp_path),
        executor=lambda *_: {"returncode": 0, "stdout": "ok", "stderr": ""},
        ops=ops,
    )

    assert result["status"] == "success"
    assert len(prompts) == 1
    assert bootstrap_calls["count"] == 2
    assert "No module named pip" not in prompts[0]


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


def test_default_executor_prefers_workspace_venv_python(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    venv_bin = tmp_path / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    venv_python = venv_bin / "python"
    venv_python.write_text("#!/bin/sh\n", encoding="utf-8")
    venv_python.chmod(0o755)

    def fake_run(command, **kwargs):
        captured["command"] = list(command)
        captured.update(kwargs)

        class _Result:
            returncode = 0
            stdout = "ok"
            stderr = ""

        return _Result()

    import subprocess

    monkeypatch.setattr(
        shutil,
        "which",
        lambda value: None if value in {"python", "python3"} else f"/usr/bin/{value}",
    )
    monkeypatch.setattr(subprocess, "run", fake_run)

    result = _default_executor("python -m ruff check .", str(tmp_path))

    assert captured["command"] == [str(venv_python), "-m", "ruff", "check", "."]
    env = captured["env"]
    assert isinstance(env, dict)
    assert env["VIRTUAL_ENV"] == str(tmp_path / ".venv")
    assert str(venv_bin) == str(env["PATH"]).split(os.pathsep)[0]
    assert result.returncode == 0


def test_bootstrap_workspace_runtime_installs_python_requirements_once_per_signature(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("fastapi\n", encoding="utf-8")
    legacy_state = tmp_path / agent_runner.LEGACY_BOOTSTRAP_STATE_FILENAME
    legacy_state.write_text("legacy", encoding="utf-8")
    state_file = tmp_path / ".git" / "software-factory" / "bootstrap-state.json"
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        argv = list(command)
        calls.append(argv)
        if argv[1:3] == ["-m", "venv"]:
            venv_python = tmp_path / ".venv" / "bin" / "python"
            venv_python.parent.mkdir(parents=True, exist_ok=True)
            venv_python.write_text("#!/bin/sh\n", encoding="utf-8")
            venv_python.chmod(0o755)

        class _Result:
            returncode = 0
            stdout = "ok"
            stderr = ""

        return _Result()

    monkeypatch.setattr(agent_runner.sys, "executable", "/usr/bin/python3")
    monkeypatch.setattr(agent_runner.subprocess, "run", fake_run)
    monkeypatch.setattr(
        agent_runner, "_bootstrap_state_file", lambda workspace: state_file
    )

    commands = ["python -m ruff check ."]
    first = agent_runner._bootstrap_workspace_runtime(str(tmp_path), commands=commands)
    second = agent_runner._bootstrap_workspace_runtime(str(tmp_path), commands=commands)

    assert first.ok is True
    assert first.skipped is False
    assert second.ok is True
    assert second.skipped is True
    assert state_file.is_file()
    assert legacy_state.exists() is False
    assert calls == [
        ["/usr/bin/python3", "-m", "venv", str(tmp_path / ".venv")],
        [
            str(tmp_path / ".venv" / "bin" / "python"),
            "-m",
            "pip",
            "install",
            "-r",
            str(requirements),
        ],
        [
            str(tmp_path / ".venv" / "bin" / "python"),
            "-m",
            "pip",
            "install",
            "ruff",
        ],
    ]


def test_resolve_repo_git_dir_prefers_git_rev_parse_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = tmp_path / ".git"
    expected.mkdir()

    monkeypatch.setattr(
        agent_runner,
        "_run_git_command",
        lambda **kwargs: agent_runner.subprocess.CompletedProcess(
            args=["git", "rev-parse", "--absolute-git-dir"],
            returncode=0,
            stdout=str(expected),
            stderr="",
        ),
    )

    assert agent_runner._resolve_repo_git_dir(tmp_path) == expected.resolve()


def test_resolve_repo_git_dir_falls_back_to_worktree_git_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gitdir = tmp_path / ".git-dir"
    gitdir.mkdir()
    (tmp_path / ".git").write_text("gitdir: .git-dir\n", encoding="utf-8")

    monkeypatch.setattr(
        agent_runner,
        "_run_git_command",
        lambda **kwargs: agent_runner.subprocess.CompletedProcess(
            args=["git", "rev-parse", "--absolute-git-dir"],
            returncode=1,
            stdout="",
            stderr="fatal: not a git repository",
        ),
    )

    assert agent_runner._resolve_repo_git_dir(tmp_path) == gitdir.resolve()


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
            "error_stage": "git_push",
            "remote": "origin",
            "branch": "feature/test",
            "pushed_ref": "origin/feature/test",
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
    posted_comments: list[str] = []

    ops = RunnerOps(
        checkout_branch=lambda *_: (True, "checked out"),
        ensure_head_sha=lambda *_: True,
        commit_and_push=lambda **_: {
            "success": True,
            "commit_sha": "deadbeef",
            "error": None,
        },
        post_pr_comment=lambda *_args: (
            posted_comments.append(str(_args[3])) or True,
            "ok",
        ),
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
    assert result["comment_posted"] is True
    assert len(posted_comments) == 1
    assert "Status: failed" in posted_comments[0]
    assert "ai_not_configured" in posted_comments[0]


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

    monkeypatch.setattr(
        agent_runner,
        "_prepare_run_workspace",
        lambda **kwargs: (str(tmp_path), None, "feature/test", "abc123"),
    )

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


def test_execute_agent_sdks_falls_back_to_claude(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fake_openhands(
        workspace: str,
        run_id: int,
        repo: str,
        pr_number: int,
        prompt: str,
        normalized_review: dict[str, object],
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
        normalized_review: dict[str, object],
        *,
        command: str,
        provider: str,
        base_url: str,
        model: str,
        runtime: str,
        container_image: str,
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
        normalized_review={},
        modes=("openhands", "claude_agent_sdk"),
        openhands_command="openhands",
        openhands_command_timeout_seconds=600,
        claude_agent_command="claude",
        claude_agent_provider="openrouter",
        claude_agent_base_url="https://openrouter.ai/api",
        claude_agent_model="openrouter/hunter-alpha",
        claude_agent_runtime="host",
        claude_agent_container_image="",
        claude_agent_command_timeout_seconds=600,
    )
    assert ok is True
    assert err_code is None
    assert err_message is None
    assert selected_mode == "claude_agent_sdk"
    assert calls == ["/tmp", "/tmp"]


def test_execute_agent_sdks_does_not_fall_back_to_openhands_after_claude_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fake_claude(
        workspace: str,
        run_id: int,
        repo: str,
        pr_number: int,
        prompt: str,
        normalized_review: dict[str, object],
        *,
        command: str,
        provider: str,
        base_url: str,
        model: str,
        runtime: str,
        container_image: str,
        timeout_seconds: int,
        on_log_line: object | None = None,
        should_cancel: object | None = None,
    ) -> tuple[bool, str, str | None]:
        calls.append("claude")
        return False, "claude failed", "agent_claude_failed"

    def fake_openhands(**kwargs) -> tuple[bool, str, str | None]:
        calls.append("openhands")
        return True, "openhands succeeded", None

    monkeypatch.setattr(agent_runner, "_run_claude_agent", fake_claude)
    monkeypatch.setattr(agent_runner, "_run_openhands_agent", fake_openhands)

    ok, err_code, err_message, selected_mode = agent_runner._execute_agent_sdks(
        workspace="/tmp",
        run_id=123,
        repo="owner/repo",
        pr_number=1,
        prompt="fix this",
        normalized_review={},
        modes=("claude_agent_sdk", "openhands"),
        openhands_command="openhands",
        openhands_command_timeout_seconds=600,
        claude_agent_command="claude",
        claude_agent_provider="openrouter",
        claude_agent_base_url="https://openrouter.ai/api",
        claude_agent_model="openrouter/hunter-alpha",
        claude_agent_runtime="host",
        claude_agent_container_image="",
        claude_agent_command_timeout_seconds=600,
    )

    assert ok is False
    assert err_code == "agent_claude_failed"
    assert err_message == "claude failed"
    assert selected_mode == "claude_agent_sdk"
    assert calls == ["claude"]


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
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-openrouter-key")
    monkeypatch.setenv("GITHUB_PERSONAL_ACCESS_TOKEN", "test-gh-pat")
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
        normalized_review={},
        command="  claude --print  ",
        provider="openrouter",
        base_url="https://openrouter.ai/api",
        model="openrouter/hunter-alpha",
        runtime="host",
        container_image="",
        timeout_seconds=42,
    )

    assert ok is True
    assert message == "done"
    assert error_code is None
    assert captured["command"] == [
        "claude",
        "--print",
        "--verbose",
        "--permission-mode",
        "auto",
        "--allowed-tools",
        "Bash,Read,Edit,Glob,Grep,LS,WebFetch",
        "--output-format",
        "stream-json",
        "fix this",
    ]
    assert captured["cwd"] == str(tmp_path)
    assert captured["stdout"] == agent_runner.subprocess.PIPE
    assert captured["stderr"] == agent_runner.subprocess.PIPE
    assert captured["stdin"] == agent_runner.subprocess.DEVNULL
    assert captured["text"] is True
    assert captured["timeout"] == 42
    assert captured["input"] is None
    env = captured["env"]
    assert isinstance(env, dict)
    assert env["OPENAI_API_KEY"] == "test-openai-key"
    assert env["OPENROUTER_API_KEY"] == "test-openrouter-key"
    assert env["ANTHROPIC_BASE_URL"] == "https://openrouter.ai/api"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "test-openrouter-key"
    assert env["ANTHROPIC_API_KEY"] == ""
    assert env["ANTHROPIC_MODEL"] == "openrouter/hunter-alpha"
    assert env["ANTHROPIC_SMALL_FAST_MODEL"] == "openrouter/hunter-alpha"
    assert env["GH_TOKEN"] == "test-gh-pat"
    assert env["GITHUB_TOKEN"] == "test-gh-pat"
    assert env["SOFTWARE_FACTORY_REPO"] == "acme/widgets"
    assert env["SOFTWARE_FACTORY_PR_NUMBER"] == "7"
    assert env["SOFTWARE_FACTORY_RUN_ID"] == "9"
    assert "UNRELATED_SECRET" not in env


def test_run_claude_agent_supports_docker_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}

    class _FakeProcess:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self.returncode = 0
            self.pid = 1001
            captured["command"] = list(args[0]) if args else []
            captured.update(kwargs)

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

    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-openrouter-key")
    monkeypatch.setenv("GITHUB_PERSONAL_ACCESS_TOKEN", "test-gh-pat")
    monkeypatch.setattr(agent_runner.subprocess, "Popen", _FakeProcess)
    monkeypatch.setattr(
        agent_runner.shutil,
        "which",
        lambda value: "/usr/bin/docker" if value == "docker" else f"/usr/bin/{value}",
    )
    gitdir = tmp_path / ".git-dir"
    gitdir.mkdir()
    (tmp_path / ".git").write_text(f"gitdir: {gitdir}\n", encoding="utf-8")
    (gitdir / "commondir").write_text("../.git-common\n", encoding="utf-8")
    git_common = tmp_path / ".git-common"
    git_common.mkdir()

    ok, message, error_code = _run_claude_agent(
        workspace=str(tmp_path),
        run_id=9,
        repo="acme/widgets",
        pr_number=7,
        prompt="fix this",
        normalized_review={},
        command="claude",
        provider="openrouter",
        base_url="https://openrouter.ai/api",
        model="openrouter/hunter-alpha",
        runtime="docker",
        container_image="ghcr.io/example/claude-code:latest",
        timeout_seconds=42,
    )

    assert ok is True
    assert message == "done"
    assert error_code is None
    assert captured["command"][:8] == [
        "docker",
        "run",
        "--rm",
        "-i",
        "--workdir",
        "/workspace",
        "--volume",
        f"{tmp_path.resolve()}:/workspace",
    ]
    assert "ghcr.io/example/claude-code:latest" in captured["command"]
    assert f"{gitdir}:{gitdir}" in captured["command"]
    assert f"{git_common.resolve()}:{git_common.resolve()}" in captured["command"]
    assert "--env" in captured["command"]
    assert "OPENAI_API_KEY" in captured["command"]
    assert "test-openai-key" not in captured["command"]
    assert "--allowed-tools" in captured["command"]
    assert captured["cwd"] == str(tmp_path)
    assert captured["stdin"] == agent_runner.subprocess.DEVNULL
    assert captured["input"] is None
    assert captured["command"][-1] == "fix this"
    env = captured["env"]
    assert isinstance(env, dict)
    assert env["OPENAI_API_KEY"] == "test-openai-key"
    assert env["OPENROUTER_API_KEY"] == "test-openrouter-key"
    assert env["ANTHROPIC_BASE_URL"] == "https://openrouter.ai/api"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "test-openrouter-key"
    assert env["ANTHROPIC_API_KEY"] == ""
    assert env["GH_TOKEN"] == "test-gh-pat"
    assert env["GITHUB_TOKEN"] == "test-gh-pat"
    assert env["HOME"] == "/tmp/claude-home"


def test_build_workspace_git_mounts_ignores_missing_git_metadata(
    tmp_path: Path,
) -> None:
    assert agent_runner._build_workspace_git_mounts(str(tmp_path)) == []


def test_run_claude_agent_rejects_shell_control_tokens(tmp_path: Path) -> None:
    ok, message, error_code = _run_claude_agent(
        workspace=str(tmp_path),
        run_id=9,
        repo="acme/widgets",
        pr_number=7,
        prompt="fix this",
        normalized_review={},
        command="claude && whoami",
        provider="openrouter",
        base_url="https://openrouter.ai/api",
        model="openrouter/hunter-alpha",
        runtime="host",
        container_image="",
        timeout_seconds=42,
    )

    assert ok is False
    assert error_code == "agent_claude_failed"
    assert "unsupported shell control operators" in message


def test_sanitize_log_text_redacts_tokens() -> None:
    raw = (
        "token=abc123 secret: xyz ghp_abcdefghijklmnopqrstuvwxyz "
        "OPENAI_API_KEY=test-openai-key"
    )
    masked = _sanitize_log_text(raw)
    assert "abc123" not in masked
    assert "xyz" not in masked
    assert "ghp_abcdefghijklmnopqrstuvwxyz" not in masked
    assert "test-openai-key" not in masked
    assert "[REDACTED]" in masked

def test_render_claude_stream_record_handles_non_object_json() -> None:
    lines, result_text, error_text, saw_events = _render_claude_stream_record(
        '["a", "b"]\n'
    )

    assert lines == ["[agent][stdout] ['a', 'b']"]
    assert result_text is None
    assert error_text is None
    assert saw_events is False


def test_consume_claude_stream_falls_back_when_renderer_raises() -> None:
    class _Stream:
        def __init__(self) -> None:
            self._lines = iter(['{"type":"assistant"}\n', ""])
            self.closed = False

        def readline(self) -> str:
            return next(self._lines)

        def close(self) -> None:
            self.closed = True

    lines: list[str] = []
    chunks: list[str] = []
    state: dict[str, object] = {}
    stream = _Stream()

    original = agent_runner._render_claude_stream_record

    def _boom(raw_line: str):
        raise RuntimeError(f"boom: {raw_line}")

    try:
        agent_runner._render_claude_stream_record = _boom
        _consume_claude_stream(stream, chunks, lines.append, state)
    finally:
        agent_runner._render_claude_stream_record = original

    assert chunks == ['{"type":"assistant"}\n']
    assert lines == ['[agent][stdout] {"type":"assistant"}']
    assert state == {}
    assert stream.closed is True


def test_build_run_progress_callback_updates_file_database_from_worker_thread(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "app.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    run_id = enqueue_autofix_run(
        conn=conn,
        repo="acme/widgets",
        pr_number=42,
        head_sha="abc123",
        normalized_review_json={"summary": "1 blocking issue"},
    )
    assert run_id is not None

    callback = _build_run_progress_callback(conn, run_id)
    assert callback is not None

    worker = threading.Thread(
        target=lambda: callback("logs/autofix-run-42.log"),
    )
    worker.start()
    worker.join()

    row = conn.execute(
        "SELECT logs_path FROM autofix_runs WHERE id = ?",
        (run_id,),
    ).fetchone()
    assert row is not None
    assert row["logs_path"] == "logs/autofix-run-42.log"


def test_build_agent_env_includes_ci_context() -> None:
    env = _build_agent_env(
        run_id=19,
        repo="acme/widgets",
        pr_number=24,
        normalized_review={
            "ci_status": "failed",
            "ci_checks": [
                {
                    "source": "workflow_run",
                    "name": "CI / unit",
                    "status": "completed",
                    "conclusion": "failure",
                    "details_url": "https://example.test/runs/1",
                    "head_sha": "abc123",
                },
                {
                    "source": "check_run",
                    "name": "lint",
                    "status": "completed",
                    "conclusion": "success",
                    "details_url": "https://example.test/runs/2",
                    "head_sha": "abc123",
                },
            ],
        },
    )

    assert env["SOFTWARE_FACTORY_REPO"] == "acme/widgets"
    assert env["SOFTWARE_FACTORY_PR_NUMBER"] == "24"
    assert env["SOFTWARE_FACTORY_RUN_ID"] == "19"
    assert env["SOFTWARE_FACTORY_CI_STATUS"] == "failed"
    assert env["SOFTWARE_FACTORY_CI_FAILED_CHECKS"] == "CI / unit"
    assert '"name": "CI / unit"' in env["SOFTWARE_FACTORY_CI_CHECKS_JSON"]
