from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
import re
import shlex
import sqlite3
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from typing import Any, Callable, Mapping

from app.config import get_settings
from app.services.agent_prompt import (
    build_autofix_prompt,
    collect_check_commands,
    summarize_check_results,
)
from app.services.concurrency import acquire_pr_lock, release_pr_lock
from app.services.git_ops import (
    checkout_branch,
    commit_and_push,
    ensure_head_sha,
    post_pr_comment,
)
from app.services.logging_config import cleanup_archived_logs, get_run_log_path
from app.services.feature_flags import resolve_agent_feature_flags
from app.services.policy import increment_autofix_count
from app.services.queue import (
    get_run_status,
    is_run_cancel_requested,
    mark_run_finished,
    update_run_logs_path,
)
from app.services.retry import RetryConfig, schedule_retry


Executor = Callable[[str, str], Any]
CHECK_COMMAND_TIMEOUT_SECONDS = 300
GIT_COMMAND_TIMEOUT_SECONDS = 30
WORKTREE_CMD_PREFIX = "sf-autofix-openhands"
OPENHANDS_AGENT_MODE = "openhands"
CLAUDE_AGENT_MODE = "claude_agent_sdk"
OPENHANDS_FAILURE_CODE_WORKTREE = "agent_worktree_failed"
OPENHANDS_FAILURE_CODE_COMMAND = "agent_openhands_failed"
CLAUDE_FAILURE_CODE_COMMAND = "agent_claude_failed"
RUN_CANCELLED_CODE = "cancelled"

_REDACTION_PATTERNS = (
    re.compile(r"(ghp_[A-Za-z0-9]{16,})"),
    re.compile(r"(github_pat_[A-Za-z0-9_]{20,})"),
    re.compile(r"(?i)(token\s*[=:]\s*)([^\s]+)"),
    re.compile(r"(?i)(secret\s*[=:]\s*)([^\s]+)"),
)
_ANSI_CSI_PATTERN = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")
_ANSI_OSC_PATTERN = re.compile(r"\x1B\].*?(?:\x07|\x1B\\)")
_ANSI_ESC_PATTERN = re.compile(r"\x1B[@-_]")
_C0_CONTROL_PATTERN = re.compile(r"[\x00-\x08\x0B-\x1F\x7F]")
_ALLOWED_AGENT_ENV_KEYS = {
    "HOME",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "LOGNAME",
    "NO_PROXY",
    "PATH",
    "PYTHONPATH",
    "REQUESTS_CA_BUNDLE",
    "SHELL",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
    "TEMP",
    "TERM",
    "TMP",
    "TMPDIR",
    "USER",
    "VIRTUAL_ENV",
    "https_proxy",
    "http_proxy",
    "no_proxy",
}
_ALLOWED_AGENT_ENV_PREFIXES = (
    "ANTHROPIC_",
    "AWS_",
    "AZURE_OPENAI_",
    "BIGMODEL_",
    "CLAUDE_",
    "DASHSCOPE_",
    "DEEPSEEK_",
    "GEMINI_",
    "GITHUB_",
    "GH_",
    "GOOGLE_",
    "LANGCHAIN_",
    "LANGSMITH_",
    "LITELLM_",
    "MISTRAL_",
    "MODEL_",
    "MOONSHOT_",
    "OPENAI_",
    "OPENCODE_",
    "OPENROUTER_",
    "QWEN_",
    "XAI_",
    "ZHIPU_",
)
_DISALLOWED_COMMAND_TOKENS = {"&", "&&", ";", "<", "<<", ">", ">>", "|", "||"}
_ACTIVE_AGENT_PIDS_LOCK = threading.Lock()
_ACTIVE_AGENT_PIDS: set[int] = set()


def _noop(*_args: Any, **_kwargs: Any) -> Any:
    return None


@dataclass(frozen=True)
class RunnerOps:
    checkout_branch: Callable[[str, str], tuple[bool, str]] = checkout_branch
    ensure_head_sha: Callable[[str, str], bool] = ensure_head_sha
    commit_and_push: Callable[..., dict[str, Any]] = commit_and_push
    post_pr_comment: Callable[[str, str, int, str], tuple[bool, str]] = post_pr_comment
    generate_fix: Callable[..., Any] = _noop
    apply_fix_plan: Callable[..., Any] = _noop
    build_autofix_prompt: Callable[..., str] = build_autofix_prompt
    collect_check_commands: Callable[[str | None], list[str]] = collect_check_commands
    summarize_check_results: Callable[[list[dict[str, Any]]], dict[str, Any]] = (
        summarize_check_results
    )


@dataclass
class RunLogger:
    workspace_dir: str
    run_id: int
    lines: list[str]
    logs_path: str = field(init=False)

    def __post_init__(self) -> None:
        self.logs_path = _write_logs(self.workspace_dir, self.run_id, self.lines)

    def append(self, line: str) -> None:
        self.lines.append(line)
        _append_logs(self.logs_path, [line])

    def extend(self, new_lines: list[str]) -> None:
        if not new_lines:
            return
        self.lines.extend(new_lines)
        _append_logs(self.logs_path, new_lines)

    def flush(self) -> str:
        self.logs_path = _write_logs(self.workspace_dir, self.run_id, self.lines)
        return self.logs_path


def run_once(
    conn: sqlite3.Connection,
    run: dict[str, Any],
    workspace_dir: str,
    executor: Executor | None = None,
    ops: RunnerOps | None = None,
) -> dict[str, Any]:
    settings = get_settings()
    active_ops = ops or RunnerOps()
    workspace = _validate_workspace_dir(workspace_dir)
    run_id = int(run["id"])
    repo = str(run.get("repo") or "")
    pr_number = int(run.get("pr_number") or 0)
    worker_id = _safe_text(run.get("worker_id")) or settings.worker_id

    payload = _parse_payload(run.get("normalized_review_json"))
    head_sha = _safe_text(run.get("head_sha")) or _safe_text(payload.get("head_sha"))
    branch = _resolve_branch(conn, run, payload)
    project_type = _safe_text(payload.get("project_type"))
    commit_message = _safe_text(payload.get("commit_message")) or (
        f"fix: apply autofix updates for PR #{pr_number}"
    )
    feature_flags = resolve_agent_feature_flags(conn)

    prompt = active_ops.build_autofix_prompt(
        repo=repo,
        pr_number=pr_number,
        head_sha=head_sha or "unknown",
        normalized_review=payload,
    )
    commands = active_ops.collect_check_commands(project_type)
    cleanup_archived_logs(
        base_dir=workspace,
        archive_subdir=settings.log_archive_subdir,
        older_than_days=settings.log_retention_days,
    )
    if not commands:
        error_summary = f"unsupported_project_type: {project_type or 'unknown'}"
        logs_path = _write_logs(
            workspace_dir=workspace,
            run_id=run_id,
            lines=[
                f"run_id={run_id}",
                f"repo={repo}",
                f"pr_number={pr_number}",
                error_summary,
            ],
        )
        status, scheduled_error = _finish_failed_run(
            conn,
            run_id,
            error_summary,
            logs_path,
            error_code="unsupported_project_type",
        )
        return {
            "run_id": run_id,
            "status": status,
            "error_summary": scheduled_error,
            "logs_path": logs_path,
            "commit_sha": None,
            "checks": {
                "overall_status": "failed",
                "passed_count": 0,
                "failed_count": 0,
                "failed_commands": [],
            },
            "comment_posted": False,
        }

    execute = _default_executor if executor is None else executor
    agent_modes = _normalize_agent_modes(feature_flags.agent_sdks)
    check_results: list[dict[str, Any]] = []
    checks_summary = {
        "overall_status": "failed",
        "passed_count": 0,
        "failed_count": 0,
        "failed_commands": [],
    }
    log_lines = [
        f"run_id={run_id}",
        f"repo={repo}",
        f"pr_number={pr_number}",
        f"head_sha={head_sha or 'unknown'}",
        f"branch={branch or 'unknown'}",
        f"agent_modes={','.join(agent_modes)}",
        "prompt:",
        prompt,
        "",
    ]
    logger = RunLogger(workspace_dir=workspace, run_id=run_id, lines=log_lines)
    update_run_logs_path(conn, run_id, logger.logs_path)
    lock_acquired = acquire_pr_lock(
        conn=conn,
        repo=repo,
        pr_number=pr_number,
        lock_owner=worker_id,
        lock_ttl_seconds=settings.pr_lock_ttl_seconds,
        run_id=run_id,
    )
    if not lock_acquired:
        logger.append("pr_lock: already held")
        logs_path = logger.logs_path
        status, error_summary = _finish_failed_run(
            conn,
            run_id,
            "pr_locked",
            logs_path,
            error_code="pr_locked",
        )
        return {
            "run_id": run_id,
            "status": status,
            "error_summary": error_summary,
            "logs_path": logs_path,
            "commit_sha": None,
            "checks": {
                "overall_status": "failed",
                "passed_count": 0,
                "failed_count": 0,
                "failed_commands": [],
            },
            "comment_posted": False,
        }

    if is_run_cancel_requested(conn, run_id):
        logger.append("cancel_requested: stopping run before execution")
        logs_path = logger.flush()
        status, run_error_summary = _finish_cancelled_run(
            conn,
            run_id,
            logs_path,
        )
        return {
            "run_id": run_id,
            "status": status,
            "error_summary": run_error_summary,
            "logs_path": logs_path,
            "commit_sha": None,
            "checks": checks_summary,
            "comment_posted": False,
        }

    agent_workspace = workspace
    agent_worktree: str | None = None
    if OPENHANDS_AGENT_MODE in agent_modes or CLAUDE_AGENT_MODE in agent_modes:
        primary_agent_mode = agent_modes[0]
        worktree_base_dir = (
            feature_flags.openhands_worktree_base_dir
            if primary_agent_mode == OPENHANDS_AGENT_MODE
            else feature_flags.claude_agent_worktree_base_dir
        )
        try:
            agent_workspace, agent_worktree = _prepare_openhands_workspace(
                base_repo_dir=workspace,
                run_id=run_id,
                branch=branch,
                head_sha=head_sha,
                worktree_base_dir=worktree_base_dir,
            )
            logger.append(f"agent_workspace={agent_workspace}")
        except ValueError as exc:
            workspace_error = f"agent workspace init failed: {exc}"
            logger.append(workspace_error)
            if CLAUDE_AGENT_MODE not in agent_modes:
                logs_path = logger.logs_path
                status, scheduled_error = _finish_failed_run(
                    conn=conn,
                    run_id=run_id,
                    error_summary=workspace_error,
                    logs_path=logs_path,
                    error_code=OPENHANDS_FAILURE_CODE_WORKTREE,
                )
                return {
                    "run_id": run_id,
                    "status": status,
                    "error_summary": scheduled_error,
                    "logs_path": logs_path,
                    "commit_sha": None,
                    "checks": {
                        "overall_status": "failed",
                        "passed_count": 0,
                        "failed_count": 0,
                        "failed_commands": [],
                    },
                    "comment_posted": False,
                }

    try:
        status = "failed"
        run_error_summary = None
        run_error_code: str | None = None
        commit_sha: str | None = None
        check_workspace = workspace

        sdk_ok, sdk_error_code, sdk_error_message, used_agent_mode = _execute_agent_sdks(
            workspace=agent_workspace,
            run_id=run_id,
            repo=repo,
            pr_number=pr_number,
            prompt=prompt,
            modes=agent_modes,
            openhands_command=feature_flags.openhands_command,
            openhands_command_timeout_seconds=(
                feature_flags.openhands_command_timeout_seconds
            ),
            claude_agent_command=feature_flags.claude_agent_command,
            claude_agent_command_timeout_seconds=(
                feature_flags.claude_agent_command_timeout_seconds
            ),
            on_log_line=logger.append,
            should_cancel=lambda: is_run_cancel_requested(conn, run_id),
        )
        if used_agent_mode in {OPENHANDS_AGENT_MODE, CLAUDE_AGENT_MODE}:
            check_workspace = agent_workspace
        logger.append(f"agent_mode={used_agent_mode or 'unknown'}")
        if sdk_error_message:
            logger.append(f"agent_error: {sdk_error_message}")

        if not sdk_ok:
            if sdk_error_code == RUN_CANCELLED_CODE:
                logs_path = logger.flush()
                status, run_error_summary = _finish_cancelled_run(
                    conn,
                    run_id,
                    logs_path,
                )
                return {
                    "run_id": run_id,
                    "status": status,
                    "error_summary": run_error_summary,
                    "logs_path": logs_path,
                    "commit_sha": None,
                    "checks": checks_summary,
                    "comment_posted": False,
                }
            failure_summary = (
                f"{sdk_error_code}: {sdk_error_message}"
                if sdk_error_code and sdk_error_message
                and not str(sdk_error_message).startswith(f"{sdk_error_code}:")
                else sdk_error_message
            )
            logs_path = logger.logs_path
            status, run_error_summary = _finish_failed_run(
                conn=conn,
                run_id=run_id,
                error_summary=failure_summary or "agent_sdk_failed",
                logs_path=logs_path,
                error_code=sdk_error_code or "agent_sdk_failed",
            )
            return {
                "run_id": run_id,
                "status": status,
                "error_summary": run_error_summary,
                "logs_path": logs_path,
                "commit_sha": None,
                "checks": {
                    "overall_status": "failed",
                    "passed_count": 0,
                    "failed_count": 0,
                    "failed_commands": [],
                },
                "comment_posted": False,
            }

        if run_error_summary is None:
            for command in commands:
                if is_run_cancel_requested(conn, run_id):
                    logger.append("cancel_requested: stopping run before checks")
                    logs_path = logger.flush()
                    status, run_error_summary = _finish_cancelled_run(
                        conn,
                        run_id,
                        logs_path,
                    )
                    return {
                        "run_id": run_id,
                        "status": status,
                        "error_summary": run_error_summary,
                        "logs_path": logs_path,
                        "commit_sha": None,
                        "checks": checks_summary,
                        "comment_posted": False,
                    }
                result = _coerce_result(execute(command, check_workspace))
                check_results.append(
                    {
                        "command": command,
                        "exit_code": result["returncode"],
                        "stdout": result["stdout"],
                        "stderr": result["stderr"],
                    }
                )
                logger.extend(
                    [
                        f"[check] {command}",
                        f"exit_code={result['returncode']}",
                        "stdout:",
                        _sanitize_log_text(result["stdout"]),
                        "stderr:",
                        _sanitize_log_text(result["stderr"]),
                        "",
                    ]
                )

            checks_summary = active_ops.summarize_check_results(check_results)

            if checks_summary["overall_status"] != "passed":
                failed_commands = checks_summary.get("failed_commands") or []
                run_error_summary = (
                    f"checks_failed: {', '.join(str(item) for item in failed_commands)}"
                )
                logger.append(run_error_summary)
            else:
                status, commit_sha, run_error_summary = _finalize_git_changes(
                    repo_dir=check_workspace,
                    commit_message=commit_message,
                    active_ops=active_ops,
                    log_lines=log_lines,
                )
                logger.flush()

        logs_path = logger.flush()
        if status == "success":
            increment_autofix_count(
                conn,
                repo,
                pr_number,
                branch=branch,
                head_sha=head_sha,
            )
            mark_run_finished(
                conn=conn,
                run_id=run_id,
                status=status,
                commit_sha=commit_sha,
                error_summary=run_error_summary,
                logs_path=logs_path,
            )
        else:
            status, run_error_summary = _finish_failed_run(
                conn,
                run_id,
                run_error_summary or "unknown_failure",
                logs_path,
                error_code=run_error_code or _infer_error_code(run_error_summary),
            )
    finally:
        if agent_worktree is not None:
            _cleanup_openhands_workspace(
                base_repo_dir=workspace,
                worktree_dir=agent_worktree,
            )
        release_pr_lock(
            conn,
            repo,
            pr_number,
            lock_owner=worker_id,
            run_id=run_id,
            force=False,
        )

    if status == "retry_scheduled":
        return {
            "run_id": run_id,
            "status": status,
            "error_summary": run_error_summary,
            "logs_path": logs_path,
            "commit_sha": commit_sha,
            "checks": checks_summary,
            "comment_posted": False,
        }

    comment_body = _build_pr_comment(
        run_id=run_id,
        status=status,
        summary=checks_summary,
        commit_sha=commit_sha,
        error_summary=run_error_summary,
        logs_path=logs_path,
    )
    posted, comment_message = active_ops.post_pr_comment(
        workspace,
        repo,
        pr_number,
        comment_body,
    )
    if not posted:
        comment_failure = f"pr_comment_failed: {comment_message}"
        logger.append(comment_failure)
        run_error_summary = _merge_error_summary(run_error_summary, comment_failure)
        mark_run_finished(
            conn=conn,
            run_id=run_id,
            status=status,
            commit_sha=commit_sha,
            error_summary=run_error_summary,
            logs_path=logs_path,
        )

    return {
        "run_id": run_id,
        "status": status,
        "error_summary": run_error_summary,
        "logs_path": logs_path,
        "commit_sha": commit_sha,
        "checks": checks_summary,
        "comment_posted": posted,
    }


def _finalize_git_changes(
    repo_dir: str,
    commit_message: str,
    active_ops: RunnerOps,
    log_lines: list[str],
) -> tuple[str, str | None, str | None]:
    commit_result = active_ops.commit_and_push(
        repo_dir=repo_dir,
        message=commit_message,
    )
    if commit_result.get("success"):
        commit_sha = _safe_text(commit_result.get("commit_sha"))
        log_lines.append(f"git_push: success commit={commit_sha or 'unknown'}")
        return "success", commit_sha, None

    error = _safe_text(commit_result.get("error")) or "unknown_git_error"
    if error == "no_changes":
        log_lines.append("git_push: skipped no_changes")
        return "success", None, None

    log_lines.append(f"git_push: failed error={error}")
    return "failed", _safe_text(commit_result.get("commit_sha")), f"git_failed: {error}"


def _normalize_agent_modes(raw_modes: tuple[str, ...]) -> tuple[str, ...]:
    normalized: list[str] = []
    for mode in raw_modes:
        value = mode.strip().lower()
        if not value:
            continue
        if value == "legacy":
            value = CLAUDE_AGENT_MODE
        if value not in {OPENHANDS_AGENT_MODE, CLAUDE_AGENT_MODE}:
            continue
        if value in normalized:
            continue
        normalized.append(value)
    if not normalized:
        return (CLAUDE_AGENT_MODE, OPENHANDS_AGENT_MODE)
    return tuple(normalized)


def _execute_agent_sdks(
    *,
    workspace: str,
    run_id: int,
    repo: str,
    pr_number: int,
    prompt: str,
    modes: tuple[str, ...],
    openhands_command: str,
    openhands_command_timeout_seconds: int,
    claude_agent_command: str,
    claude_agent_command_timeout_seconds: int,
    on_log_line: Callable[[str], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> tuple[bool, str | None, str | None, str | None]:
    last_error_code: str | None = None
    last_error_message: str | None = None
    for mode in modes:
        if mode == OPENHANDS_AGENT_MODE:
            openhands_ok, openhands_message, openhands_error_code = _run_openhands_agent(
                workspace=workspace,
                run_id=run_id,
                repo=repo,
                pr_number=pr_number,
                prompt=prompt,
                command=openhands_command,
                timeout_seconds=openhands_command_timeout_seconds,
                on_log_line=on_log_line,
                should_cancel=should_cancel,
            )
            if openhands_ok:
                return True, None, None, OPENHANDS_AGENT_MODE

            last_error_code = openhands_error_code
            last_error_message = openhands_message
            continue

        if mode == CLAUDE_AGENT_MODE:
            claude_ok, claude_message, claude_error_code = _run_claude_agent(
                workspace=workspace,
                run_id=run_id,
                repo=repo,
                pr_number=pr_number,
                prompt=prompt,
                command=claude_agent_command,
                timeout_seconds=claude_agent_command_timeout_seconds,
                on_log_line=on_log_line,
                should_cancel=should_cancel,
            )
            if claude_ok:
                return True, None, None, CLAUDE_AGENT_MODE

            last_error_code = claude_error_code
            last_error_message = claude_message
            continue

    return False, last_error_code, last_error_message, None


def _run_openhands_agent(
    workspace: str,
    run_id: int,
    repo: str,
    pr_number: int,
    prompt: str,
    *,
    command: str,
    timeout_seconds: int,
    on_log_line: Callable[[str], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> tuple[bool, str, str | None]:
    return _run_agent_command(
        workspace=workspace,
        run_id=run_id,
        repo=repo,
        pr_number=pr_number,
        prompt=prompt,
        command=command,
        timeout_seconds=timeout_seconds,
        agent_name="OpenHands",
        failure_code=OPENHANDS_FAILURE_CODE_COMMAND,
        on_log_line=on_log_line,
        should_cancel=should_cancel,
    )


def _run_claude_agent(
    workspace: str,
    run_id: int,
    repo: str,
    pr_number: int,
    prompt: str,
    *,
    command: str,
    timeout_seconds: int,
    on_log_line: Callable[[str], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> tuple[bool, str, str | None]:
    return _run_agent_command(
        workspace=workspace,
        run_id=run_id,
        repo=repo,
        pr_number=pr_number,
        prompt=prompt,
        command=command,
        timeout_seconds=timeout_seconds,
        agent_name="Claude Agent SDK",
        failure_code=CLAUDE_FAILURE_CODE_COMMAND,
        on_log_line=on_log_line,
        should_cancel=should_cancel,
    )


def _run_agent_command(
    *,
    workspace: str,
    run_id: int,
    repo: str,
    pr_number: int,
    prompt: str,
    command: str,
    timeout_seconds: int,
    agent_name: str,
    failure_code: str,
    on_log_line: Callable[[str], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> tuple[bool, str, str | None]:
    normalized_command = command.strip()
    if not normalized_command:
        return False, f"{agent_name} command is not configured", failure_code

    try:
        argv = shlex.split(normalized_command)
    except ValueError as exc:
        return False, f"{agent_name} command is invalid: {exc}", failure_code
    if not argv:
        return False, f"{agent_name} command is not configured", failure_code
    if any(token in _DISALLOWED_COMMAND_TOKENS for token in argv[1:]):
        return (
            False,
            f"{agent_name} command contains unsupported shell control operators",
            failure_code,
        )
    if not _command_exists(argv[0]):
        return False, f"{agent_name} command not found: {argv[0]}", failure_code

    process: subprocess.Popen[str]
    try:
        process = subprocess.Popen(
            argv,
            cwd=workspace,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.PIPE,
            text=True,
            env=_build_agent_environment(repo=repo, pr_number=pr_number, run_id=run_id),
            start_new_session=True,
        )
    except FileNotFoundError:
        return False, f"{agent_name} command not found: {argv[0]}", failure_code
    except OSError as exc:
        return (
            False,
            f"{agent_name} command failed to start: {exc}",
            failure_code,
        )

    _register_active_agent_process(process.pid)
    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []
    if on_log_line is not None:
        on_log_line(f"[agent] starting {agent_name}: {normalized_command}")
    stdout_thread = threading.Thread(
        target=_consume_process_stream,
        args=(process.stdout, "stdout", stdout_chunks, on_log_line),
        daemon=True,
    )
    stderr_thread = threading.Thread(
        target=_consume_process_stream,
        args=(process.stderr, "stderr", stderr_chunks, on_log_line),
        daemon=True,
    )
    stdout_thread.start()
    stderr_thread.start()
    try:
        if process.stdin is not None:
            process.stdin.write(prompt)
            process.stdin.close()
        deadline = time.monotonic() + timeout_seconds
        while True:
            if should_cancel is not None and should_cancel():
                if on_log_line is not None:
                    on_log_line("[agent] cancellation requested; terminating process")
                _terminate_agent_process_tree(process)
                return False, f"{agent_name} command cancelled by user", RUN_CANCELLED_CODE
            if process.poll() is not None:
                break
            if time.monotonic() >= deadline:
                _terminate_agent_process_tree(process)
                return False, f"{agent_name} command timed out after {timeout_seconds}s", failure_code
            time.sleep(1.0)
    except OSError as exc:
        return False, f"{agent_name} command failed while running: {exc}", failure_code
    finally:
        stdout_thread.join(timeout=1.0)
        stderr_thread.join(timeout=1.0)
        _unregister_active_agent_process(process.pid)

    stdout = "".join(stdout_chunks)
    stderr = "".join(stderr_chunks)

    if process.returncode != 0:
        std_err = (stderr or "").strip()
        std_out = (stdout or "").strip()
        message = std_err or std_out or f"{agent_name} command failed"
        return False, message, failure_code

    return True, (stdout or "").strip() or f"{agent_name} completed", None


def _consume_process_stream(
    stream: Any,
    stream_name: str,
    chunks: list[str],
    on_log_line: Callable[[str], None] | None,
) -> None:
    if stream is None:
        return
    try:
        for raw_line in iter(stream.readline, ""):
            chunks.append(raw_line)
            rendered = _clean_terminal_log_line(raw_line.rstrip("\n"))
            if on_log_line is not None and rendered:
                on_log_line(f"[agent][{stream_name}] {rendered}")
    finally:
        stream.close()


def _register_active_agent_process(pid: int | None) -> None:
    if pid is None:
        return
    with _ACTIVE_AGENT_PIDS_LOCK:
        _ACTIVE_AGENT_PIDS.add(int(pid))


def _unregister_active_agent_process(pid: int | None) -> None:
    if pid is None:
        return
    with _ACTIVE_AGENT_PIDS_LOCK:
        _ACTIVE_AGENT_PIDS.discard(int(pid))


def cleanup_active_agent_processes() -> None:
    with _ACTIVE_AGENT_PIDS_LOCK:
        pids = tuple(_ACTIVE_AGENT_PIDS)

    for pid in pids:
        _terminate_agent_process_tree_by_pid(pid)


def _terminate_agent_process_tree(process: subprocess.Popen[str]) -> None:
    if process.pid is None:
        return
    _terminate_agent_process_tree_by_pid(process.pid)


def _terminate_agent_process_tree_by_pid(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        with _ACTIVE_AGENT_PIDS_LOCK:
            _ACTIVE_AGENT_PIDS.discard(pid)
        return
    except OSError:
        return

    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError:
        pass

    with _ACTIVE_AGENT_PIDS_LOCK:
        _ACTIVE_AGENT_PIDS.discard(pid)


def _build_agent_environment(*, repo: str, pr_number: int, run_id: int) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if key in _ALLOWED_AGENT_ENV_KEYS
        or any(key.startswith(prefix) for prefix in _ALLOWED_AGENT_ENV_PREFIXES)
    }
    env["SOFTWARE_FACTORY_REPO"] = repo
    env["SOFTWARE_FACTORY_PR_NUMBER"] = str(pr_number)
    env["SOFTWARE_FACTORY_RUN_ID"] = str(run_id)
    return env


def _command_exists(command_name: str) -> bool:
    if not command_name:
        return False
    if os.path.sep in command_name:
        return Path(command_name).expanduser().exists()
    return shutil.which(command_name) is not None


def _prepare_openhands_workspace(
    *,
    base_repo_dir: str,
    run_id: int,
    branch: str | None,
    head_sha: str | None,
    worktree_base_dir: str,
) -> tuple[str, str]:
    base_repo = Path(base_repo_dir)
    if not (base_repo / ".git").exists():
        raise ValueError("agent workspace requires a git repository")

    git_ref = branch or head_sha or "HEAD"
    worktree_root = Path(worktree_base_dir)
    if not worktree_root.is_absolute():
        worktree_root = base_repo / worktree_root

    worktree_root.mkdir(parents=True, exist_ok=True)
    worktree_dir = tempfile.mkdtemp(
        prefix=f"{WORKTREE_CMD_PREFIX}-{run_id}-", dir=str(worktree_root)
    )
    try:
        result = _run_git_command(
            repo_dir=base_repo_dir,
            args=["worktree", "add", "--detach", worktree_dir, git_ref],
            timeout=GIT_COMMAND_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        shutil.rmtree(worktree_dir)
        raise ValueError(f"failed to create worktree: {exc}") from exc

    if result.returncode != 0:
        shutil.rmtree(worktree_dir)
        details = result.stderr.strip() or result.stdout.strip() or "unknown git error"
        raise ValueError(f"git worktree add failed: {details}")

    return worktree_dir, worktree_dir


def _cleanup_openhands_workspace(base_repo_dir: str, worktree_dir: str) -> None:
    try:
        _run_git_command(
            repo_dir=base_repo_dir,
            args=["worktree", "remove", "--force", worktree_dir],
            timeout=GIT_COMMAND_TIMEOUT_SECONDS,
        )
    finally:
        shutil.rmtree(worktree_dir, ignore_errors=True)


def _run_git_command(
    repo_dir: str,
    args: list[str],
    *,
    timeout: int = 30,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo_dir,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _prepare_workspace(
    repo_dir: str,
    branch: str | None,
    head_sha: str | None,
    active_ops: RunnerOps,
    log_lines: list[str],
) -> str | None:
    if branch:
        ok, checkout_message = active_ops.checkout_branch(repo_dir, branch)
        log_lines.append(f"checkout: {checkout_message}")
        if not ok:
            return f"checkout_failed: {checkout_message}"

    if head_sha and not active_ops.ensure_head_sha(repo_dir, head_sha):
        log_lines.append("head_sha_check: mismatch")
        return "head_sha_mismatch"

    return None


def _write_logs(workspace_dir: str, run_id: int, lines: list[str]) -> str:
    logs_file = get_run_log_path(
        workspace_dir,
        run_id,
        relative_dir=get_settings().log_dir,
    )
    logs_file.write_text("\n".join(lines), encoding="utf-8")
    return str(logs_file)


def _append_logs(logs_path: str, lines: list[str]) -> None:
    if not lines:
        return
    path = Path(logs_path)
    prefix = "\n" if path.exists() and path.stat().st_size > 0 else ""
    with path.open("a", encoding="utf-8") as handle:
        handle.write(prefix)
        handle.write("\n".join(lines))


def _clean_terminal_log_line(value: str) -> str:
    text = _ANSI_OSC_PATTERN.sub("", value)
    text = _ANSI_CSI_PATTERN.sub("", text)
    text = _ANSI_ESC_PATTERN.sub("", text)
    text = _C0_CONTROL_PATTERN.sub("", text)
    return _sanitize_log_text(text).strip()


def _finish_cancelled_run(
    conn: sqlite3.Connection,
    run_id: int,
    logs_path: str,
) -> tuple[str, str]:
    current_status = get_run_status(conn, run_id) or "cancelled"
    error_summary = (
        "cancel_requested_by_user"
        if current_status == "cancel_requested"
        else "cancelled_by_user"
    )
    mark_run_finished(
        conn=conn,
        run_id=run_id,
        status="cancelled",
        error_summary=error_summary,
        logs_path=logs_path,
        last_error_code=RUN_CANCELLED_CODE,
    )
    return "cancelled", error_summary


def _finish_failed_run(
    conn: sqlite3.Connection,
    run_id: int,
    error_summary: str,
    logs_path: str,
    *,
    error_code: str,
) -> tuple[str, str]:
    settings = get_settings()
    config = RetryConfig(
        base_delay_seconds=settings.retry_backoff_base_seconds,
        max_delay_seconds=settings.retry_backoff_max_seconds,
        non_retryable_error_codes=set(settings.non_retryable_error_codes),
    )
    plan = schedule_retry(
        conn,
        run_id,
        error_code=error_code,
        error_summary=error_summary,
        config=config,
    )
    status = "retry_scheduled" if plan.scheduled else "failed"
    if not plan.scheduled:
        try:
            mark_run_finished(
                conn=conn,
                run_id=run_id,
                status="failed",
                error_summary=error_summary,
                logs_path=logs_path,
                last_error_code=error_code,
            )
            return status, error_summary
        except sqlite3.Error:
            conn.rollback()
            conn.execute(
                """
                UPDATE autofix_runs
                SET status = 'failed',
                    error_summary = ?,
                    logs_path = ?,
                    last_error_code = COALESCE(?, last_error_code),
                    last_error_at = CURRENT_TIMESTAMP,
                    finished_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (error_summary, logs_path, error_code, run_id),
            )
            conn.commit()
            return status, error_summary

    try:
        conn.execute(
            "UPDATE autofix_runs SET logs_path = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (logs_path, run_id),
        )
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        return status, f"{error_summary}; db_update_failed_for_retry={run_id}"

    return status, f"{error_summary}; retry_after={plan.retry_after}"


def _infer_error_code(error_summary: str | None) -> str:
    if not error_summary:
        return "unknown_failure"
    if ":" in error_summary:
        return error_summary.split(":", 1)[0].strip() or "unknown_failure"
    return error_summary.strip() or "unknown_failure"


def _build_pr_comment(
    run_id: int,
    status: str,
    summary: Mapping[str, Any],
    commit_sha: str | None,
    error_summary: str | None,
    logs_path: str,
) -> str:
    lines = [
        f"Autofix run #{run_id}",
        f"Status: {status}",
        f"Checks: {summary.get('passed_count', 0)} passed, {summary.get('failed_count', 0)} failed",
    ]
    if commit_sha:
        lines.append(f"Commit: {commit_sha}")
    if error_summary:
        lines.append(f"Error: {error_summary}")
    lines.append(f"Logs: {logs_path}")
    return "\n".join(lines)


def _resolve_branch(
    conn: sqlite3.Connection, run: Mapping[str, Any], payload: Mapping[str, Any]
) -> str | None:
    from_payload = _safe_text(payload.get("branch"))
    if from_payload:
        return from_payload

    repo = _safe_text(run.get("repo"))
    pr_number = run.get("pr_number")
    if not repo or not isinstance(pr_number, int):
        return None

    row = conn.execute(
        "SELECT branch FROM pull_requests WHERE repo = ? AND pr_number = ? LIMIT 1",
        (repo, pr_number),
    ).fetchone()
    if row is None:
        return None
    if hasattr(row, "keys"):
        return _safe_text(row["branch"])
    if isinstance(row, tuple) and row:
        return _safe_text(row[0])
    return None


def _parse_payload(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        if isinstance(parsed, dict):
            return parsed
    return {}


def _default_executor(
    command: str, workspace_dir: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        shlex.split(command),
        cwd=workspace_dir,
        check=False,
        capture_output=True,
        text=True,
        timeout=CHECK_COMMAND_TIMEOUT_SECONDS,
    )


def _coerce_result(result: Any) -> dict[str, Any]:
    if isinstance(result, dict):
        return {
            "returncode": int(result.get("returncode", result.get("exit_code", 0))),
            "stdout": str(result.get("stdout", "")),
            "stderr": str(result.get("stderr", "")),
        }

    return {
        "returncode": int(getattr(result, "returncode", 0)),
        "stdout": str(getattr(result, "stdout", "")),
        "stderr": str(getattr(result, "stderr", "")),
    }


def _safe_text(value: Any) -> str | None:
    if isinstance(value, str):
        text = value.strip()
        if text:
            return text
    return None


def _validate_workspace_dir(workspace_dir: str) -> str:
    resolved = Path(workspace_dir).expanduser().resolve()
    if not resolved.exists() or not resolved.is_dir():
        raise ValueError(f"Invalid workspace_dir: {workspace_dir}")
    return str(resolved)


def _sanitize_log_text(text: str) -> str:
    sanitized = text
    sanitized = _REDACTION_PATTERNS[0].sub("[REDACTED]", sanitized)
    sanitized = _REDACTION_PATTERNS[1].sub("[REDACTED]", sanitized)
    sanitized = _REDACTION_PATTERNS[2].sub(
        lambda m: f"{m.group(1)}[REDACTED]", sanitized
    )
    sanitized = _REDACTION_PATTERNS[3].sub(
        lambda m: f"{m.group(1)}[REDACTED]", sanitized
    )
    return sanitized


def _merge_error_summary(existing: str | None, new_error: str) -> str:
    if not existing:
        return new_error
    return f"{existing}; {new_error}"
