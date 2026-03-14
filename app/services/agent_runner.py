from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
import re
import shlex
import sqlite3
import shutil
import subprocess
import tempfile
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
from app.services.queue import mark_run_finished
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

_REDACTION_PATTERNS = (
    re.compile(r"(ghp_[A-Za-z0-9]{16,})"),
    re.compile(r"(github_pat_[A-Za-z0-9_]{20,})"),
    re.compile(r"(?i)(token\s*[=:]\s*)([^\s]+)"),
    re.compile(r"(?i)(secret\s*[=:]\s*)([^\s]+)"),
)


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
    lock_acquired = acquire_pr_lock(
        conn=conn,
        repo=repo,
        pr_number=pr_number,
        lock_owner=worker_id,
        lock_ttl_seconds=settings.pr_lock_ttl_seconds,
        run_id=run_id,
    )
    if not lock_acquired:
        log_lines.append("pr_lock: already held")
        logs_path = _write_logs(workspace_dir=workspace, run_id=run_id, lines=log_lines)
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

    agent_workspace = workspace
    agent_worktree: str | None = None
    if OPENHANDS_AGENT_MODE in agent_modes or CLAUDE_AGENT_MODE in agent_modes:
        worktree_base_dir = (
            feature_flags.openhands_worktree_base_dir
            if OPENHANDS_AGENT_MODE in agent_modes
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
            log_lines.append(f"agent_workspace={agent_workspace}")
        except ValueError as exc:
            workspace_error = f"agent workspace init failed: {exc}"
            log_lines.append(workspace_error)
            if CLAUDE_AGENT_MODE not in agent_modes:
                logs_path = _write_logs(
                    workspace_dir=workspace,
                    run_id=run_id,
                    lines=log_lines,
                )
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
        )
        if used_agent_mode in {OPENHANDS_AGENT_MODE, CLAUDE_AGENT_MODE}:
            check_workspace = agent_workspace
        log_lines.append(f"agent_mode={used_agent_mode or 'unknown'}")
        if sdk_error_message:
            log_lines.append(f"agent_error: {sdk_error_message}")

        if not sdk_ok:
            failure_summary = (
                f"{sdk_error_code}: {sdk_error_message}"
                if sdk_error_code and sdk_error_message
                and not str(sdk_error_message).startswith(f"{sdk_error_code}:")
                else sdk_error_message
            )
            logs_path = _write_logs(
                workspace_dir=workspace,
                run_id=run_id,
                lines=log_lines,
            )
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
                result = _coerce_result(execute(command, check_workspace))
                check_results.append(
                    {
                        "command": command,
                        "exit_code": result["returncode"],
                        "stdout": result["stdout"],
                        "stderr": result["stderr"],
                    }
                )
                log_lines.extend(
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
                log_lines.append(run_error_summary)
            else:
                status, commit_sha, run_error_summary = _finalize_git_changes(
                    repo_dir=check_workspace,
                    commit_message=commit_message,
                    active_ops=active_ops,
                    log_lines=log_lines,
                )

        logs_path = _write_logs(
            workspace_dir=workspace,
            run_id=run_id,
            lines=log_lines,
        )
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
        log_lines.append(comment_failure)
        _write_logs(workspace_dir=workspace, run_id=run_id, lines=log_lines)
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
    has_openhands = False
    has_claude = False
    for mode in raw_modes:
        value = mode.strip().lower()
        if not value:
            continue
        if value not in {OPENHANDS_AGENT_MODE, CLAUDE_AGENT_MODE, "legacy"}:
            continue
        if value == OPENHANDS_AGENT_MODE:
            has_openhands = True
        elif value in {CLAUDE_AGENT_MODE, "legacy"}:
            has_claude = True
    if not (has_openhands or has_claude):
        return (OPENHANDS_AGENT_MODE,)
    normalized: list[str] = []
    if has_openhands:
        normalized.append(OPENHANDS_AGENT_MODE)
    if has_claude:
        normalized.append(CLAUDE_AGENT_MODE)
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
) -> tuple[bool, str, str | None]:
    normalized_command = command.strip()
    if not normalized_command:
        return (
            False,
            "OpenHands command is not configured",
            OPENHANDS_FAILURE_CODE_COMMAND,
        )

    env = os.environ.copy()
    env["SOFTWARE_FACTORY_REPO"] = repo
    env["SOFTWARE_FACTORY_PR_NUMBER"] = str(pr_number)
    env["SOFTWARE_FACTORY_RUN_ID"] = str(run_id)
    try:
        result = subprocess.run(
            shlex.split(command),
            cwd=workspace,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            input=prompt,
            env=env,
        )
    except FileNotFoundError:
        return (
            False,
            f"OpenHands command not found: {normalized_command}",
            OPENHANDS_FAILURE_CODE_COMMAND,
        )
    except subprocess.TimeoutExpired:
        return (
            False,
            f"OpenHands command timed out after {timeout_seconds}s",
            OPENHANDS_FAILURE_CODE_COMMAND,
        )

    if result.returncode != 0:
        std_err = result.stderr.strip()
        std_out = result.stdout.strip()
        message = std_err or std_out or "OpenHands command failed"
        return False, message, OPENHANDS_FAILURE_CODE_COMMAND

    return True, result.stdout.strip() or "OpenHands completed", None


def _run_claude_agent(
    workspace: str,
    run_id: int,
    repo: str,
    pr_number: int,
    prompt: str,
    *,
    command: str,
    timeout_seconds: int,
) -> tuple[bool, str, str | None]:
    normalized_command = command.strip()
    if not normalized_command:
        return (
            False,
            "Claude Agent SDK command is not configured",
            CLAUDE_FAILURE_CODE_COMMAND,
        )

    env = os.environ.copy()
    env["SOFTWARE_FACTORY_REPO"] = repo
    env["SOFTWARE_FACTORY_PR_NUMBER"] = str(pr_number)
    env["SOFTWARE_FACTORY_RUN_ID"] = str(run_id)
    try:
        result = subprocess.run(
            shlex.split(command),
            cwd=workspace,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            input=prompt,
            env=env,
        )
    except FileNotFoundError:
        return (
            False,
            f"Claude Agent SDK command not found: {normalized_command}",
            CLAUDE_FAILURE_CODE_COMMAND,
        )
    except subprocess.TimeoutExpired:
        return (
            False,
            f"Claude Agent SDK command timed out after {timeout_seconds}s",
            CLAUDE_FAILURE_CODE_COMMAND,
        )

    if result.returncode != 0:
        std_err = result.stderr.strip()
        std_out = result.stdout.strip()
        message = std_err or std_out or "Claude Agent SDK command failed"
        return False, message, CLAUDE_FAILURE_CODE_COMMAND

    return True, result.stdout.strip() or "Claude Agent SDK completed", None


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
