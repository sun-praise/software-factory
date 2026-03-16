from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
import re
import shlex
import sqlite3
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Callable, Mapping

import httpx

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
    touch_run_progress,
    update_run_logs_path,
)
from app.services.retry import RetryConfig, schedule_retry


Executor = Callable[[str, str], Any]
CHECK_COMMAND_TIMEOUT_SECONDS = 300
BOOTSTRAP_COMMAND_TIMEOUT_SECONDS = 600
GIT_COMMAND_TIMEOUT_SECONDS = 30
MAX_CHECK_FEEDBACK_ATTEMPTS = 3
WORKTREE_CMD_PREFIX = "sf-autofix-openhands"
OPENHANDS_AGENT_MODE = "openhands"
CLAUDE_AGENT_MODE = "claude_agent_sdk"
OPENHANDS_FAILURE_CODE_WORKTREE = "agent_worktree_failed"
OPENHANDS_FAILURE_CODE_COMMAND = "agent_openhands_failed"
CLAUDE_FAILURE_CODE_COMMAND = "agent_claude_failed"
RUN_CANCELLED_CODE = "cancelled"
BOOTSTRAP_STATE_FILENAME = ".software_factory_bootstrap_state.json"
PR_FETCH_TIMEOUT_SECONDS = 30

_REDACTION_PATTERNS = (
    re.compile(r"(ghp_[A-Za-z0-9]{16,})"),
    re.compile(r"(github_pat_[A-Za-z0-9_]{20,})"),
    re.compile(r"(?i)(token\s*[=:]\s*)([^\s]+)"),
    re.compile(r"(?i)(secret\s*[=:]\s*)([^\s]+)"),
    re.compile(r"(?i)([A-Z0-9_]*(?:TOKEN|SECRET|API_KEY)[A-Z0-9_]*=)([^\s]+)"),
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
logger = logging.getLogger(__name__)


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


@dataclass(frozen=True)
class WorkspaceBootstrapPlan:
    kind: str
    manifest_paths: tuple[Path, ...]
    commands: tuple[tuple[str, ...], ...]
    ready_paths: tuple[Path, ...]


@dataclass(frozen=True)
class WorkspaceBootstrapResult:
    ok: bool
    skipped: bool
    kind: str | None = None
    details: tuple[dict[str, Any], ...] = ()
    error_summary: str | None = None


@dataclass
class RunLogger:
    workspace_dir: str
    run_id: int
    lines: list[str]
    on_progress: Callable[[str], None] | None = None
    logs_path: str = field(init=False)

    def __post_init__(self) -> None:
        self.logs_path = _write_logs(self.workspace_dir, self.run_id, self.lines)
        self._notify_progress()

    def append(self, line: str) -> None:
        self.lines.append(line)
        _append_logs(self.logs_path, [line])
        self._notify_progress()

    def extend(self, new_lines: list[str]) -> None:
        if not new_lines:
            return
        self.lines.extend(new_lines)
        _append_logs(self.logs_path, new_lines)
        self._notify_progress()

    def flush(self) -> str:
        self.logs_path = _write_logs(self.workspace_dir, self.run_id, self.lines)
        self._notify_progress()
        return self.logs_path

    def _notify_progress(self) -> None:
        if self.on_progress is not None:
            self.on_progress(self.logs_path)


def run_once(
    conn: sqlite3.Connection,
    run: dict[str, Any],
    workspace_dir: str,
    executor: Executor | None = None,
    ops: RunnerOps | None = None,
) -> dict[str, Any]:
    settings = get_settings()
    active_ops = ops or RunnerOps()
    runtime_root = _validate_runtime_root(workspace_dir)
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
        base_dir=runtime_root,
        archive_subdir=settings.log_archive_subdir,
        older_than_days=settings.log_retention_days,
    )
    if not commands:
        error_summary = f"unsupported_project_type: {project_type or 'unknown'}"
        logs_path = _write_logs(
            workspace_dir=runtime_root,
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
    logger = RunLogger(
        workspace_dir=runtime_root,
        run_id=run_id,
        lines=log_lines,
        on_progress=_build_run_progress_callback(conn, run_id),
    )
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

    agent_workspace = runtime_root
    agent_worktree: str | None = None
    if OPENHANDS_AGENT_MODE in agent_modes or CLAUDE_AGENT_MODE in agent_modes:
        try:
            agent_workspace, agent_worktree, branch, head_sha = _prepare_run_workspace(
                runtime_root=runtime_root,
                repo=repo,
                pr_number=pr_number,
                run_id=run_id,
                branch=branch,
                head_sha=head_sha,
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
        check_workspace = runtime_root
        baseline_check_workspace = agent_workspace
        try:
            baseline_check_results, baseline_checks_summary = _run_validation_cycle(
                conn=conn,
                run_id=run_id,
                workspace_dir=baseline_check_workspace,
                commands=commands,
                execute=execute,
                logger=logger,
                log_prefix="baseline",
            )
        except RuntimeError as exc:
            if str(exc) != "cancel_requested_before_checks":
                raise
            logger.append("cancel_requested: stopping run before baseline checks")
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
        baseline_failure_index = _build_check_failure_index(baseline_check_results)
        if baseline_failure_index:
            logger.append(
                "preexisting_check_failures: "
                + ", ".join(sorted(baseline_failure_index))
            )

        prompt_for_attempt = prompt
        for attempt in range(1, MAX_CHECK_FEEDBACK_ATTEMPTS + 1):
            logger.append(
                f"agent_attempt={attempt}/{MAX_CHECK_FEEDBACK_ATTEMPTS}"
            )
            sdk_ok, sdk_error_code, sdk_error_message, used_agent_mode = _execute_agent_sdks(
                workspace=agent_workspace,
                run_id=run_id,
                repo=repo,
                pr_number=pr_number,
                prompt=prompt_for_attempt,
                modes=agent_modes,
                openhands_command=feature_flags.openhands_command,
                openhands_command_timeout_seconds=(
                    feature_flags.openhands_command_timeout_seconds
                ),
                claude_agent_command=feature_flags.claude_agent_command,
                claude_agent_provider=feature_flags.claude_agent_provider,
                claude_agent_base_url=feature_flags.claude_agent_base_url,
                claude_agent_model=feature_flags.claude_agent_model,
                claude_agent_runtime=feature_flags.claude_agent_runtime,
                claude_agent_container_image=(
                    feature_flags.claude_agent_container_image
                ),
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

            try:
                check_results, checks_summary = _run_validation_cycle(
                    conn=conn,
                    run_id=run_id,
                    workspace_dir=check_workspace,
                    commands=commands,
                    execute=execute,
                    logger=logger,
                )
            except RuntimeError as exc:
                if str(exc) != "cancel_requested_before_checks":
                    raise
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
            new_failure_results = _filter_new_check_failures(
                baseline_check_results=baseline_check_results,
                current_check_results=check_results,
            )
            if checks_summary["overall_status"] == "passed" or not new_failure_results:
                preexisting_error_summary: str | None = None
                if checks_summary["overall_status"] != "passed":
                    preexisting_failed_commands = checks_summary.get("failed_commands") or []
                    preexisting_error_summary = (
                        "preexisting_checks_failed: "
                        + ", ".join(str(item) for item in preexisting_failed_commands)
                    )
                    logger.append(preexisting_error_summary)
                    checks_summary = {
                        **checks_summary,
                        "overall_status": "passed",
                        "failed_count": 0,
                        "failed_commands": [],
                    }
                status, commit_sha, git_error_summary = _finalize_git_changes(
                    repo_dir=check_workspace,
                    commit_message=commit_message,
                    active_ops=active_ops,
                    log_lines=log_lines,
                )
                run_error_summary = preexisting_error_summary or git_error_summary
                logger.flush()
                break

            failed_commands = checks_summary.get("failed_commands") or []
            run_error_summary = (
                f"checks_failed: {', '.join(str(item) for item in failed_commands)}"
            )
            logger.append(run_error_summary)
            if attempt >= MAX_CHECK_FEEDBACK_ATTEMPTS:
                break
            prompt_for_attempt = _build_check_feedback_prompt(
                base_prompt=prompt,
                check_results=new_failure_results,
            )
            logger.append("agent_feedback: rerunning agent with failed check output")

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
                runtime_root=runtime_root,
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
        runtime_root,
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
        pushed_ref = _safe_text(commit_result.get("pushed_ref")) or "unknown"
        log_lines.append(
            f"git_push: success ref={pushed_ref} commit={commit_sha or 'unknown'}"
        )
        return "success", commit_sha, None

    error = _safe_text(commit_result.get("error")) or "unknown_git_error"
    if error == "no_changes":
        log_lines.append("git_push: skipped no_changes")
        return "success", None, None

    error_stage = _safe_text(commit_result.get("error_stage")) or "git"
    pushed_ref = _safe_text(commit_result.get("pushed_ref"))
    if pushed_ref:
        log_lines.append(
            f"git_push: failed stage={error_stage} ref={pushed_ref} error={error}"
        )
    else:
        log_lines.append(f"git_push: failed stage={error_stage} error={error}")
    error_prefix = "git_push_failed" if error_stage == "git_push" else "git_commit_failed"
    return "failed", _safe_text(commit_result.get("commit_sha")), f"{error_prefix}: {error}"


def _run_validation_cycle(
    *,
    conn: sqlite3.Connection,
    run_id: int,
    workspace_dir: str,
    commands: list[str],
    execute: Executor,
    logger: RunLogger,
    log_prefix: str = "check",
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    bootstrap_result = _bootstrap_workspace_runtime(workspace_dir)
    if bootstrap_result.kind:
        logger.append(f"{log_prefix}_workspace_bootstrap={bootstrap_result.kind}")
    if bootstrap_result.skipped and bootstrap_result.kind:
        logger.append(f"{log_prefix}_workspace_bootstrap_status=ready")
    for detail in bootstrap_result.details:
        logger.extend(
            [
                f"[{log_prefix}-bootstrap] {detail['command']}",
                f"exit_code={detail['exit_code']}",
                "stdout:",
                _sanitize_log_text(detail["stdout"]),
                "stderr:",
                _sanitize_log_text(detail["stderr"]),
                "",
            ]
        )

    check_results: list[dict[str, Any]] = []
    bootstrap_failed = False
    if not bootstrap_result.ok:
        bootstrap_failed = True
        if bootstrap_result.details:
            check_results.extend(
                {
                    "command": f"[bootstrap] {detail['command']}",
                    "exit_code": detail["exit_code"],
                    "stdout": detail["stdout"],
                    "stderr": detail["stderr"],
                }
                for detail in bootstrap_result.details
            )
        else:
            check_results.append(
                {
                    "command": "[bootstrap] workspace setup",
                    "exit_code": 1,
                    "stdout": "",
                    "stderr": bootstrap_result.error_summary or "",
                }
            )

    for command in commands:
        if bootstrap_failed:
            break
        if is_run_cancel_requested(conn, run_id):
            raise RuntimeError("cancel_requested_before_checks")
        result = _coerce_result(execute(command, workspace_dir))
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
                f"[{log_prefix}] {command}",
                f"exit_code={result['returncode']}",
                "stdout:",
                _sanitize_log_text(result["stdout"]),
                "stderr:",
                _sanitize_log_text(result["stderr"]),
                "",
            ]
        )

    return check_results, summarize_check_results(check_results)


def _build_check_failure_index(
    check_results: list[dict[str, Any]],
) -> dict[str, set[str]]:
    failures: dict[str, set[str]] = {}
    for result in check_results:
        exit_code = int(result.get("exit_code", 0))
        if exit_code == 0:
            continue
        command = str(result.get("command", "")).strip() or "unknown command"
        signatures = {
            line for line in _extract_check_failure_signatures(result) if line.strip()
        }
        if not signatures:
            signatures = {f"exit_code={exit_code}"}
        failures[command] = signatures
    return failures


def _filter_new_check_failures(
    *,
    baseline_check_results: list[dict[str, Any]],
    current_check_results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    baseline_index = _build_check_failure_index(baseline_check_results)
    new_failures: list[dict[str, Any]] = []
    for result in current_check_results:
        exit_code = int(result.get("exit_code", 0))
        if exit_code == 0:
            continue
        command = str(result.get("command", "")).strip() or "unknown command"
        baseline_signatures = baseline_index.get(command)
        if baseline_signatures is None:
            new_failures.append(result)
            continue
        current_signatures = {
            line for line in _extract_check_failure_signatures(result) if line.strip()
        }
        if not current_signatures:
            current_signatures = {f"exit_code={exit_code}"}
        if not current_signatures.issubset(baseline_signatures):
            new_failures.append(result)
    return new_failures


def _extract_check_failure_signatures(result: dict[str, Any]) -> set[str]:
    signatures: set[str] = set()
    for key in ("stdout", "stderr"):
        raw_text = _sanitize_log_text(str(result.get(key, "")))
        for raw_line in raw_text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if re.match(r"^Found \d+ errors? in \d+ files?", line):
                continue
            if re.match(r"^Success: no issues found", line):
                continue
            signatures.add(line)
    return signatures


def _build_check_feedback_prompt(
    *,
    base_prompt: str,
    check_results: list[dict[str, Any]],
) -> str:
    lines = [
        base_prompt,
        "",
        "Validation feedback from the previous attempt:",
        "- The last changes did not pass the required checks.",
        "- Fix the failed checks below and rerun the same validation commands.",
        "",
    ]
    for result in check_results:
        exit_code = int(result.get("exit_code", 0))
        if exit_code == 0:
            continue
        command = str(result.get("command", "")).strip() or "unknown command"
        lines.extend(
            [
                f"[failed-check] {command}",
                f"exit_code={exit_code}",
                "stdout:",
                _truncate_check_feedback_text(str(result.get("stdout", ""))),
                "stderr:",
                _truncate_check_feedback_text(str(result.get("stderr", ""))),
                "",
            ]
        )
    return "\n".join(lines)


def _truncate_check_feedback_text(text: str, limit: int = 1200) -> str:
    sanitized = _sanitize_log_text(text)
    if len(sanitized) <= limit:
        return sanitized
    return f"{sanitized[:limit].rstrip()}..."


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
    claude_agent_provider: str,
    claude_agent_base_url: str,
    claude_agent_model: str,
    claude_agent_runtime: str,
    claude_agent_container_image: str,
    claude_agent_command_timeout_seconds: int,
    on_log_line: Callable[[str], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> tuple[bool, str | None, str | None, str | None]:
    last_error_code: str | None = None
    last_error_message: str | None = None
    for mode in modes:
        if mode == OPENHANDS_AGENT_MODE:
            openhands_kwargs: dict[str, Any] = {
                "workspace": workspace,
                "run_id": run_id,
                "repo": repo,
                "pr_number": pr_number,
                "prompt": prompt,
                "command": openhands_command,
                "timeout_seconds": openhands_command_timeout_seconds,
            }
            if on_log_line is not None:
                openhands_kwargs["on_log_line"] = on_log_line
            if should_cancel is not None:
                openhands_kwargs["should_cancel"] = should_cancel
            openhands_ok, openhands_message, openhands_error_code = _run_openhands_agent(
                **openhands_kwargs,
            )
            if openhands_ok:
                return True, None, None, OPENHANDS_AGENT_MODE

            last_error_code = openhands_error_code
            last_error_message = openhands_message
            continue

        if mode == CLAUDE_AGENT_MODE:
            claude_kwargs: dict[str, Any] = {
                "workspace": workspace,
                "run_id": run_id,
                "repo": repo,
                "pr_number": pr_number,
                "prompt": prompt,
                "command": claude_agent_command,
                "provider": claude_agent_provider,
                "base_url": claude_agent_base_url,
                "model": claude_agent_model,
                "runtime": claude_agent_runtime,
                "container_image": claude_agent_container_image,
                "timeout_seconds": claude_agent_command_timeout_seconds,
            }
            if on_log_line is not None:
                claude_kwargs["on_log_line"] = on_log_line
            if should_cancel is not None:
                claude_kwargs["should_cancel"] = should_cancel
            claude_ok, claude_message, claude_error_code = _run_claude_agent(
                **claude_kwargs,
            )
            if claude_ok:
                return True, None, None, CLAUDE_AGENT_MODE

            return False, claude_error_code, claude_message, CLAUDE_AGENT_MODE

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
    provider: str,
    base_url: str,
    model: str,
    runtime: str,
    container_image: str,
    timeout_seconds: int,
    on_log_line: Callable[[str], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> tuple[bool, str, str | None]:
    if runtime == "docker":
        return _run_claude_container_command(
            workspace=workspace,
            run_id=run_id,
            repo=repo,
            pr_number=pr_number,
            prompt=prompt,
            command=command,
            provider=provider,
            base_url=base_url,
            model=model,
            container_image=container_image,
            timeout_seconds=timeout_seconds,
            agent_name="Claude Agent SDK",
            failure_code=CLAUDE_FAILURE_CODE_COMMAND,
            on_log_line=on_log_line,
            should_cancel=should_cancel,
        )
    return _run_claude_stream_command(
        workspace=workspace,
        run_id=run_id,
        repo=repo,
        pr_number=pr_number,
        prompt=prompt,
        command=command,
        provider=provider,
        base_url=base_url,
        model=model,
        timeout_seconds=timeout_seconds,
        agent_name="Claude Agent SDK",
        failure_code=CLAUDE_FAILURE_CODE_COMMAND,
        on_log_line=on_log_line,
        should_cancel=should_cancel,
    )


def _run_claude_container_command(
    *,
    workspace: str,
    run_id: int,
    repo: str,
    pr_number: int,
    prompt: str,
    command: str,
    provider: str,
    base_url: str,
    model: str,
    container_image: str,
    timeout_seconds: int,
    agent_name: str,
    failure_code: str,
    on_log_line: Callable[[str], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> tuple[bool, str, str | None]:
    normalized_command = command.strip()
    if not normalized_command:
        return False, f"{agent_name} command is not configured", failure_code
    if not container_image.strip():
        return False, f"{agent_name} container image is not configured", failure_code

    try:
        inner_argv = shlex.split(normalized_command)
    except ValueError as exc:
        return False, f"{agent_name} command is invalid: {exc}", failure_code
    if not inner_argv:
        return False, f"{agent_name} command is not configured", failure_code
    if any(token in _DISALLOWED_COMMAND_TOKENS for token in inner_argv[1:]):
        return (
            False,
            f"{agent_name} command contains unsupported shell control operators",
            failure_code,
        )
    if not _command_exists("docker"):
        return False, f"{agent_name} container runtime not found: docker", failure_code

    container_env = _build_claude_container_environment(
        repo=repo,
        pr_number=pr_number,
        run_id=run_id,
        provider=provider,
        base_url=base_url,
        model=model,
    )
    argv = _build_claude_container_command_argv(
        workspace=workspace,
        container_image=container_image,
        inner_argv=inner_argv,
        container_env=container_env,
        prompt=prompt,
    )
    return _run_claude_stream_subprocess(
        workspace=workspace,
        argv=argv,
        display_argv=_build_claude_container_command_argv(
            workspace=workspace,
            container_image=container_image,
            inner_argv=inner_argv,
            container_env=container_env,
            prompt=None,
        ),
        timeout_seconds=timeout_seconds,
        agent_name=agent_name,
        failure_code=failure_code,
        on_log_line=on_log_line,
        should_cancel=should_cancel,
        process_env=container_env,
    )


def _run_claude_stream_command(
    *,
    workspace: str,
    run_id: int,
    repo: str,
    pr_number: int,
    prompt: str,
    command: str,
    provider: str,
    base_url: str,
    model: str,
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

    argv = _build_claude_stream_command_argv(argv)
    return _run_claude_stream_subprocess(
        workspace=workspace,
        argv=[*argv, prompt],
        display_argv=argv,
        timeout_seconds=timeout_seconds,
        agent_name=agent_name,
        failure_code=failure_code,
        on_log_line=on_log_line,
        should_cancel=should_cancel,
        process_env=_build_claude_agent_environment(
            repo=repo,
            pr_number=pr_number,
            run_id=run_id,
            provider=provider,
            base_url=base_url,
            model=model,
        ),
    )


def _run_claude_stream_subprocess(
    *,
    workspace: str,
    argv: list[str],
    display_argv: list[str],
    timeout_seconds: int,
    agent_name: str,
    failure_code: str,
    on_log_line: Callable[[str], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
    process_env: dict[str, str] | None = None,
) -> tuple[bool, str, str | None]:

    process: subprocess.Popen[str]
    try:
        process = subprocess.Popen(
            argv,
            cwd=workspace,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
            env=process_env,
            start_new_session=True,
        )
    except FileNotFoundError:
        return False, f"{agent_name} command not found: {argv[0]}", failure_code
    except OSError as exc:
        return False, f"{agent_name} command failed to start: {exc}", failure_code

    state: dict[str, Any] = {
        "result_text": None,
        "error_text": None,
        "saw_events": False,
    }
    _register_active_agent_process(process.pid)
    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []
    if on_log_line is not None:
        on_log_line(
            f"[agent] starting {agent_name}: {_format_command_for_log(display_argv)}"
        )
    stdout_stream = getattr(process, "stdout", None)
    stderr_stream = getattr(process, "stderr", None)
    if stdout_stream is None or stderr_stream is None:
        try:
            stdout, stderr = process.communicate(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            _terminate_agent_process_tree(process)
            _unregister_active_agent_process(process.pid)
            return False, f"{agent_name} command timed out after {timeout_seconds}s", failure_code
        except OSError as exc:
            _unregister_active_agent_process(process.pid)
            return False, f"{agent_name} command failed while running: {exc}", failure_code
        _unregister_active_agent_process(process.pid)
        if process.returncode != 0:
            message = (stderr or "").strip() or (stdout or "").strip()
            return False, message or f"{agent_name} command failed", failure_code
        return True, (stdout or "").strip() or f"{agent_name} completed", None
    stdout_thread = threading.Thread(
        target=_consume_claude_stream,
        args=(stdout_stream, stdout_chunks, on_log_line, state),
        daemon=True,
    )
    stderr_thread = threading.Thread(
        target=_consume_process_stream,
        args=(stderr_stream, "stderr", stderr_chunks, on_log_line),
        daemon=True,
    )
    stdout_thread.start()
    stderr_thread.start()
    try:
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
    result_text = _safe_text(state.get("result_text"))
    error_text = _safe_text(state.get("error_text"))

    if process.returncode != 0:
        message = error_text or (stderr or "").strip() or result_text or (stdout or "").strip()
        return False, message or f"{agent_name} command failed", failure_code

    if on_log_line is not None and state.get("saw_events"):
        on_log_line("[agent] completed")
    return True, result_text or f"{agent_name} completed", None


def _build_claude_container_command_argv(
    *,
    workspace: str,
    container_image: str,
    inner_argv: list[str],
    container_env: Mapping[str, str],
    prompt: str | None,
) -> list[str]:
    container_workspace = "/workspace"
    expanded_inner = _build_claude_stream_command_argv(inner_argv)
    argv = [
        "docker",
        "run",
        "--rm",
        "-i",
        "--workdir",
        container_workspace,
        "--volume",
        f"{Path(workspace).resolve()}:{container_workspace}",
    ]
    for host_path, container_path in _build_workspace_git_mounts(workspace):
        argv.extend(["--volume", f"{host_path}:{container_path}"])
    if hasattr(os, "getuid") and hasattr(os, "getgid"):
        argv.extend(["--user", f"{os.getuid()}:{os.getgid()}"])
    for key in sorted(container_env):
        argv.extend(["--env", key])
    argv.append(container_image.strip())
    argv.extend(expanded_inner)
    if prompt:
        argv.append(prompt)
    return argv


def _build_workspace_git_mounts(workspace: str) -> list[tuple[str, str]]:
    workspace_path = Path(workspace).resolve()
    git_entry = workspace_path / ".git"
    if not git_entry.is_file():
        return []

    try:
        git_text = git_entry.read_text(encoding="utf-8").strip()
    except OSError:
        return []
    if not git_text.startswith("gitdir: "):
        return []

    gitdir = Path(git_text.split("gitdir: ", 1)[1]).expanduser()
    if not gitdir.exists():
        return []

    mounts: list[tuple[str, str]] = [(str(gitdir), str(gitdir))]
    commondir_file = gitdir / "commondir"
    try:
        commondir_text = commondir_file.read_text(encoding="utf-8").strip()
    except OSError:
        commondir_text = ""
    if commondir_text:
        commondir = (gitdir / commondir_text).resolve()
        if commondir.exists() and str(commondir) != str(gitdir):
            mounts.append((str(commondir), str(commondir)))
    return mounts


def _build_claude_container_environment(
    *,
    repo: str,
    pr_number: int,
    run_id: int,
    provider: str,
    base_url: str,
    model: str,
) -> dict[str, str]:
    env = _build_claude_agent_environment(
        repo=repo,
        pr_number=pr_number,
        run_id=run_id,
        provider=provider,
        base_url=base_url,
        model=model,
    )
    env["HOME"] = "/tmp/claude-home"
    env["XDG_CONFIG_HOME"] = "/tmp/claude-home/.config"
    env["XDG_CACHE_HOME"] = "/tmp/claude-home/.cache"
    return env


def _format_command_for_log(argv: list[str]) -> str:
    return _sanitize_log_text(" ".join(shlex.quote(token) for token in argv))


def _build_claude_stream_command_argv(argv: list[str]) -> list[str]:
    expanded = list(argv)
    if "-p" not in expanded and "--print" not in expanded:
        expanded.append("--print")
    if "--verbose" not in expanded:
        expanded.append("--verbose")
    if "--permission-mode" not in expanded:
        expanded.extend(["--permission-mode", "auto"])
    if not any(
        token == "--allowed-tools" or token.startswith("--allowed-tools=")
        for token in expanded
    ):
        expanded.extend(
            [
                "--allowed-tools",
                "Bash,Read,Edit,Glob,Grep,LS,WebFetch",
            ]
        )
    if not any(
        token == "--output-format" or token.startswith("--output-format=")
        for token in expanded
    ):
        expanded.extend(["--output-format", "stream-json"])
    return expanded


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


def _consume_claude_stream(
    stream: Any,
    chunks: list[str],
    on_log_line: Callable[[str], None] | None,
    state: dict[str, Any],
) -> None:
    if stream is None:
        return
    try:
        for raw_line in iter(stream.readline, ""):
            chunks.append(raw_line)
            try:
                rendered_lines, result_text, error_text, saw_events = _render_claude_stream_record(
                    raw_line
                )
            except Exception:
                cleaned = _clean_terminal_log_line(raw_line.strip())
                rendered_lines = [f"[agent][stdout] {cleaned}"] if cleaned else []
                result_text = None
                error_text = None
                saw_events = False
            if result_text:
                state["result_text"] = result_text
            if error_text:
                state["error_text"] = error_text
            if saw_events:
                state["saw_events"] = True
            if on_log_line is not None:
                for line in rendered_lines:
                    on_log_line(line)
    finally:
        stream.close()


def _render_claude_stream_record(
    raw_line: str,
) -> tuple[list[str], str | None, str | None, bool]:
    stripped = raw_line.strip()
    if not stripped:
        return [], None, None, False
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        cleaned = _clean_terminal_log_line(stripped)
        return ([f"[agent][stdout] {cleaned}"] if cleaned else []), None, None, False
    if not isinstance(payload, Mapping):
        cleaned = _clean_terminal_log_line(str(payload))
        return ([f"[agent][stdout] {cleaned}"] if cleaned else []), None, None, False

    payload_type = _safe_text(payload.get("type")) or "unknown"
    if payload_type == "init":
        session_id = _safe_text(payload.get("session_id"))
        if session_id:
            return [f"[session] {session_id}"], None, None, False
        return [], None, None, False

    if payload_type == "assistant":
        return _render_claude_assistant_event(payload), None, None, True

    if payload_type == "result":
        result_text = _safe_text(payload.get("result"))
        if payload.get("is_error"):
            return (
                [f"[agent][error] {result_text}"] if result_text else [],
                result_text,
                result_text or "Claude agent reported an error",
                False,
            )
        return [], result_text, None, False

    return [], None, None, False


def _render_claude_assistant_event(payload: dict[str, Any]) -> list[str]:
    message = payload.get("message")
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    if isinstance(content, str):
        text = _clean_terminal_log_line(content)
        return [f"[assistant] {text}"] if text else []
    if not isinstance(content, list):
        return []

    lines: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        lines.extend(_render_claude_content_block(block))
    return lines


def _render_claude_content_block(block: dict[str, Any]) -> list[str]:
    block_type = _safe_text(block.get("type")) or "unknown"
    if block_type in {"thinking", "redacted_thinking"}:
        return []
    if block_type == "text":
        text = _clean_terminal_log_line(_safe_text(block.get("text")) or "")
        if not text:
            return []
        return [f"[assistant] {line}" for line in text.splitlines() if line.strip()]
    if block_type == "tool_use":
        name = _safe_text(block.get("name")) or "tool"
        tool_input = block.get("input")
        return [_render_claude_tool_use(name, tool_input)]
    if block_type == "tool_result":
        result = _summarize_tool_payload(block.get("content"))
        return [f"[tool-result] {result}"] if result else []

    fallback = _summarize_tool_payload(block)
    return [f"[assistant:{block_type}] {fallback}"] if fallback else []


def _render_claude_tool_use(name: str, tool_input: Any) -> str:
    normalized = name.strip()
    lower_name = normalized.lower()
    if not isinstance(tool_input, Mapping):
        summary = _summarize_tool_payload(tool_input)
        return f"[tool] {normalized}: {summary}" if summary else f"[tool] {normalized}"

    path = _safe_text(tool_input.get("file_path")) or _safe_text(tool_input.get("path"))
    command = _safe_text(tool_input.get("command")) or _safe_text(tool_input.get("cmd"))
    pattern = _safe_text(tool_input.get("pattern"))
    url = _safe_text(tool_input.get("url"))

    if "read" in lower_name and path:
        return f"[read] {path}"
    if any(token in lower_name for token in {"write", "edit", "multiedit"}) and path:
        return f"[write] {path}"
    if "bash" in lower_name and command:
        return f"[bash] {command}"
    if "grep" in lower_name and pattern:
        return f"[grep] {pattern}"
    if "glob" in lower_name and pattern:
        return f"[glob] {pattern}"
    if lower_name == "ls" and path:
        return f"[ls] {path}"
    if "web" in lower_name and url:
        return f"[web] {url}"
    if "todo" in lower_name:
        items = tool_input.get("todos")
        if isinstance(items, list):
            return f"[todo] {len(items)} items"
        return "[todo] update"

    summary = _summarize_tool_payload(tool_input)
    return f"[tool] {normalized}: {summary}" if summary else f"[tool] {normalized}"


def _summarize_tool_payload(value: Any, limit: int = 180) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        text = _clean_terminal_log_line(value)
        if len(text) > limit:
            return f"{text[:limit].rstrip()}..."
        return text
    try:
        rendered = json.dumps(value, ensure_ascii=True, sort_keys=True)
    except TypeError:
        rendered = str(value)
    rendered = _clean_terminal_log_line(rendered)
    if len(rendered) > limit:
        return f"{rendered[:limit].rstrip()}..."
    return rendered


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

    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        try:
            os.killpg(pid, 0)
        except ProcessLookupError:
            with _ACTIVE_AGENT_PIDS_LOCK:
                _ACTIVE_AGENT_PIDS.discard(pid)
            return
        except OSError:
            break
        time.sleep(0.1)

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


def _build_claude_agent_environment(
    *,
    repo: str,
    pr_number: int,
    run_id: int,
    provider: str,
    base_url: str,
    model: str,
) -> dict[str, str]:
    env = _build_agent_environment(repo=repo, pr_number=pr_number, run_id=run_id)
    normalized_provider = str(provider).strip().lower()
    normalized_base_url = str(base_url).strip()
    normalized_model = str(model).strip()

    if normalized_provider == "deepseek":
        deepseek_key = str(os.environ.get("DEEPSEEK_API_KEY", "")).strip()
        if deepseek_key:
            env["ANTHROPIC_AUTH_TOKEN"] = deepseek_key
            env["ANTHROPIC_API_KEY"] = deepseek_key
    else:
        openrouter_key = str(os.environ.get("OPENROUTER_API_KEY", "")).strip()
        if openrouter_key:
            env["ANTHROPIC_AUTH_TOKEN"] = openrouter_key
        env["ANTHROPIC_API_KEY"] = ""

    if normalized_base_url:
        env["ANTHROPIC_BASE_URL"] = normalized_base_url
    if normalized_model:
        env["ANTHROPIC_MODEL"] = normalized_model
        env["ANTHROPIC_SMALL_FAST_MODEL"] = normalized_model

    gh_token = (
        str(env.get("GH_TOKEN", "")).strip()
        or str(env.get("GITHUB_TOKEN", "")).strip()
        or str(env.get("GITHUB_PERSONAL_ACCESS_TOKEN", "")).strip()
        or str(env.get("GITHUB_RELEASE_TOKEN", "")).strip()
    )
    if gh_token:
        env["GH_TOKEN"] = gh_token
        env["GITHUB_TOKEN"] = gh_token
    return env


def _command_exists(command_name: str) -> bool:
    if not command_name:
        return False
    if os.path.sep in command_name:
        return Path(command_name).expanduser().exists()
    return shutil.which(command_name) is not None


def _prepare_run_workspace(
    *,
    runtime_root: str,
    repo: str,
    pr_number: int,
    run_id: int,
    branch: str | None,
    head_sha: str | None,
) -> tuple[str, str, str | None, str | None]:
    settings = get_settings()
    runtime_path = Path(runtime_root).resolve()
    cache_root = runtime_path / settings.repo_cache_base_dir
    run_workspace_root = runtime_path / settings.run_workspace_base_dir
    cache_root.mkdir(parents=True, exist_ok=True)
    run_workspace_root.mkdir(parents=True, exist_ok=True)

    remote_url = f"https://github.com/{repo}.git"
    cache_repo_dir = cache_root / f"{repo.replace('/', '__')}.git"
    if not cache_repo_dir.exists():
        result = _run_git_command(
            repo_dir=str(cache_root),
            args=["clone", "--mirror", remote_url, str(cache_repo_dir)],
            timeout=PR_FETCH_TIMEOUT_SECONDS,
        )
        if result.returncode != 0:
            details = result.stderr.strip() or result.stdout.strip() or "unknown git error"
            raise ValueError(f"git clone --mirror failed: {details}")
    else:
        _run_git_command(
            repo_dir=str(cache_repo_dir),
            args=["remote", "set-url", "origin", remote_url],
            timeout=GIT_COMMAND_TIMEOUT_SECONDS,
        )
        result = _run_git_command(
            repo_dir=str(cache_repo_dir),
            args=["fetch", "--prune", "origin"],
            timeout=PR_FETCH_TIMEOUT_SECONDS,
        )
        if result.returncode != 0:
            details = result.stderr.strip() or result.stdout.strip() or "unknown git error"
            raise ValueError(f"git fetch cache failed: {details}")

    run_workspace_dir = tempfile.mkdtemp(
        prefix=f"{WORKTREE_CMD_PREFIX}-{run_id}-", dir=str(run_workspace_root)
    )
    try:
        clone_result = _run_git_command(
            repo_dir=str(runtime_path),
            args=[
                "clone",
                "--reference-if-able",
                str(cache_repo_dir),
                remote_url,
                run_workspace_dir,
            ],
            timeout=PR_FETCH_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        shutil.rmtree(run_workspace_dir, ignore_errors=True)
        raise ValueError(f"failed to create run workspace: {exc}") from exc

    if clone_result.returncode != 0:
        shutil.rmtree(run_workspace_dir, ignore_errors=True)
        details = clone_result.stderr.strip() or clone_result.stdout.strip() or "unknown git error"
        raise ValueError(f"git clone failed: {details}")

    resolved_branch = branch
    resolved_head_sha = head_sha
    if pr_number > 0:
        pr_branch, pr_head_sha = _fetch_pull_request_head(repo=repo, pr_number=pr_number)
        resolved_branch = resolved_branch or pr_branch
        resolved_head_sha = resolved_head_sha or pr_head_sha
        if not resolved_branch:
            shutil.rmtree(run_workspace_dir, ignore_errors=True)
            raise ValueError("unable to resolve PR head branch")
    if resolved_branch:
        fetch_result = _run_git_command(
            repo_dir=run_workspace_dir,
            args=["fetch", "origin", resolved_branch],
            timeout=PR_FETCH_TIMEOUT_SECONDS,
        )
        if fetch_result.returncode != 0:
            details = fetch_result.stderr.strip() or fetch_result.stdout.strip() or "unknown git error"
            shutil.rmtree(run_workspace_dir, ignore_errors=True)
            raise ValueError(f"git fetch branch failed: {details}")
        checkout_result = _run_git_command(
            repo_dir=run_workspace_dir,
            args=["checkout", "-B", resolved_branch, f"origin/{resolved_branch}"],
            timeout=GIT_COMMAND_TIMEOUT_SECONDS,
        )
        if checkout_result.returncode != 0 and resolved_head_sha:
            checkout_result = _run_git_command(
                repo_dir=run_workspace_dir,
                args=["checkout", "-B", resolved_branch, resolved_head_sha],
                timeout=GIT_COMMAND_TIMEOUT_SECONDS,
            )
        if checkout_result.returncode != 0:
            details = checkout_result.stderr.strip() or checkout_result.stdout.strip() or "unknown git error"
            shutil.rmtree(run_workspace_dir, ignore_errors=True)
            raise ValueError(f"git checkout branch failed: {details}")
    elif resolved_head_sha:
        checkout_result = _run_git_command(
            repo_dir=run_workspace_dir,
            args=["checkout", "--detach", resolved_head_sha],
            timeout=GIT_COMMAND_TIMEOUT_SECONDS,
        )
        if checkout_result.returncode != 0:
            details = checkout_result.stderr.strip() or checkout_result.stdout.strip() or "unknown git error"
            shutil.rmtree(run_workspace_dir, ignore_errors=True)
            raise ValueError(f"git checkout head failed: {details}")

    return run_workspace_dir, run_workspace_dir, resolved_branch, resolved_head_sha


def _cleanup_openhands_workspace(runtime_root: str, worktree_dir: str) -> None:
    settings = get_settings()
    runtime_path = Path(runtime_root).resolve()
    worktree_root = (runtime_path / settings.run_workspace_base_dir).resolve()
    worktree_path = Path(worktree_dir).resolve()
    if worktree_path == runtime_path:
        logger.warning(
            "refusing to clean runtime root directly: runtime_root=%s worktree_dir=%s",
            runtime_root,
            worktree_dir,
        )
        return
    if worktree_root not in worktree_path.parents:
        logger.warning(
            "refusing to clean workspace outside run workspace root: runtime_root=%s worktree_dir=%s",
            runtime_root,
            worktree_dir,
        )
        return
    if not worktree_path.name.startswith(f"{WORKTREE_CMD_PREFIX}-"):
        logger.warning(
            "refusing to clean workspace with unexpected name: runtime_root=%s worktree_dir=%s",
            runtime_root,
            worktree_dir,
        )
        return
    shutil.rmtree(worktree_dir, ignore_errors=True)


def _fetch_pull_request_head(*, repo: str, pr_number: int) -> tuple[str | None, str | None]:
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    url = f"https://api.github.com/repos/{repo}/pulls/{pr_number}"
    header_sets = [
        {
            "Accept": "application/vnd.github+json",
            "User-Agent": "software-factory",
            **({"Authorization": f"token {token}"} if token else {}),
        },
    ]
    if token:
        header_sets.append(
            {
                "Accept": "application/vnd.github+json",
                "User-Agent": "software-factory",
            }
        )

    for headers in header_sets:
        try:
            response = httpx.get(url, headers=headers, timeout=10.0)
        except httpx.RequestError:
            continue
        if response.status_code >= 400:
            continue
        try:
            payload = response.json()
        except ValueError:
            continue
        if not isinstance(payload, dict):
            continue
        head = payload.get("head")
        if not isinstance(head, dict):
            continue
        return _safe_text(head.get("ref")) or None, _safe_text(head.get("sha")) or None
    return None, None


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
    argv = shlex.split(command)
    venv_dir = Path(workspace_dir) / ".venv"
    venv_bin_dir = venv_dir / "bin"
    venv_python = venv_bin_dir / "python"
    env = os.environ.copy()
    if venv_bin_dir.is_dir():
        current_path = env.get("PATH", "")
        env["PATH"] = (
            f"{venv_bin_dir}{os.pathsep}{current_path}"
            if current_path
            else str(venv_bin_dir)
        )
        env["VIRTUAL_ENV"] = str(venv_dir)
    if argv and argv[0] in {"python", "python3"}:
        if venv_python.exists() and os.access(venv_python, os.X_OK):
            argv[0] = str(venv_python)
        elif argv[0] == "python" and shutil.which("python") is None:
            fallback_python = sys.executable or shutil.which("python3")
            if fallback_python:
                argv[0] = fallback_python

    return subprocess.run(
        argv,
        cwd=workspace_dir,
        check=False,
        capture_output=True,
        text=True,
        timeout=CHECK_COMMAND_TIMEOUT_SECONDS,
        env=env,
    )


def _bootstrap_workspace_runtime(workspace_dir: str) -> WorkspaceBootstrapResult:
    workspace = Path(workspace_dir)
    plan = _build_workspace_bootstrap_plan(workspace)
    if plan is None:
        return WorkspaceBootstrapResult(ok=True, skipped=True)

    state_file = workspace / BOOTSTRAP_STATE_FILENAME
    signature = _compute_bootstrap_signature(plan.manifest_paths)
    if _bootstrap_state_matches(state_file, kind=plan.kind, signature=signature) and (
        _workspace_bootstrap_ready(plan)
    ):
        return WorkspaceBootstrapResult(ok=True, skipped=True, kind=plan.kind)

    details: list[dict[str, Any]] = []
    for command in plan.commands:
        command_text = shlex.join(command)
        try:
            result = subprocess.run(
                list(command),
                cwd=workspace_dir,
                check=False,
                capture_output=True,
                text=True,
                timeout=BOOTSTRAP_COMMAND_TIMEOUT_SECONDS,
            )
        except FileNotFoundError as exc:
            _clear_bootstrap_state(state_file)
            details.append(
                {
                    "command": command_text,
                    "exit_code": 127,
                    "stdout": "",
                    "stderr": str(exc),
                }
            )
            return WorkspaceBootstrapResult(
                ok=False,
                skipped=False,
                kind=plan.kind,
                details=tuple(details),
                error_summary=f"workspace_bootstrap_failed: {plan.kind}: {command_text}",
            )

        details.append(
            {
                "command": command_text,
                "exit_code": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
            }
        )
        if result.returncode != 0:
            _clear_bootstrap_state(state_file)
            return WorkspaceBootstrapResult(
                ok=False,
                skipped=False,
                kind=plan.kind,
                details=tuple(details),
                error_summary=f"workspace_bootstrap_failed: {plan.kind}: {command_text}",
            )

    if not _workspace_bootstrap_ready(plan):
        _clear_bootstrap_state(state_file)
        return WorkspaceBootstrapResult(
            ok=False,
            skipped=False,
            kind=plan.kind,
            details=tuple(details),
            error_summary=(
                f"workspace_bootstrap_failed: {plan.kind}: "
                "bootstrap output missing expected artifacts"
            ),
        )

    _write_bootstrap_state(state_file, kind=plan.kind, signature=signature)
    return WorkspaceBootstrapResult(
        ok=True,
        skipped=False,
        kind=plan.kind,
        details=tuple(details),
    )


def _build_workspace_bootstrap_plan(
    workspace: Path,
) -> WorkspaceBootstrapPlan | None:
    package_json = workspace / "package.json"
    if package_json.is_file():
        return _build_node_bootstrap_plan(workspace)

    python_manifests = [
        path
        for path in (
            workspace / "requirements.txt",
            workspace / "requirements-dev.txt",
            workspace / "requirements-test.txt",
            workspace / "pyproject.toml",
            workspace / "setup.py",
            workspace / "setup.cfg",
        )
        if path.is_file()
    ]
    if python_manifests:
        return _build_python_bootstrap_plan(workspace, tuple(python_manifests))
    return None


def _build_python_bootstrap_plan(
    workspace: Path,
    manifests: tuple[Path, ...],
) -> WorkspaceBootstrapPlan:
    venv_dir = workspace / ".venv"
    venv_python = venv_dir / "bin" / "python"
    bootstrap_python = sys.executable or shutil.which("python3") or "python3"
    commands: list[tuple[str, ...]] = []
    if not (venv_python.exists() and os.access(venv_python, os.X_OK)):
        commands.append((bootstrap_python, "-m", "venv", str(venv_dir)))

    requirements_files = [
        path
        for path in manifests
        if path.name in {"requirements.txt", "requirements-dev.txt", "requirements-test.txt"}
    ]
    if requirements_files:
        for requirements_file in requirements_files:
            commands.append(
                (
                    str(venv_python),
                    "-m",
                    "pip",
                    "install",
                    "-r",
                    str(requirements_file),
                )
            )
    else:
        commands.append((str(venv_python), "-m", "pip", "install", "-e", "."))

    return WorkspaceBootstrapPlan(
        kind="python",
        manifest_paths=manifests,
        commands=tuple(commands),
        ready_paths=(venv_python,),
    )


def _build_node_bootstrap_plan(workspace: Path) -> WorkspaceBootstrapPlan:
    package_json = workspace / "package.json"
    manifest_paths = [package_json]
    if (workspace / "pnpm-lock.yaml").is_file():
        manifest_paths.append(workspace / "pnpm-lock.yaml")
        commands = (("pnpm", "install", "--frozen-lockfile"),)
    elif (workspace / "package-lock.json").is_file():
        manifest_paths.append(workspace / "package-lock.json")
        commands = (("npm", "ci"),)
    elif (workspace / "yarn.lock").is_file():
        manifest_paths.append(workspace / "yarn.lock")
        commands = (("yarn", "install", "--frozen-lockfile"),)
    else:
        commands = (("npm", "install"),)

    return WorkspaceBootstrapPlan(
        kind="node",
        manifest_paths=tuple(manifest_paths),
        commands=commands,
        ready_paths=(workspace / "node_modules",),
    )


def _workspace_bootstrap_ready(plan: WorkspaceBootstrapPlan) -> bool:
    for path in plan.ready_paths:
        if path.is_dir():
            return True
        if path.exists() and os.access(path, os.X_OK):
            return True
    return False


def _compute_bootstrap_signature(manifest_paths: tuple[Path, ...]) -> str:
    hasher = hashlib.sha256()
    for path in sorted(manifest_paths, key=lambda item: str(item)):
        hasher.update(str(path.name).encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(path.read_bytes())
        hasher.update(b"\0")
    return hasher.hexdigest()


def _bootstrap_state_matches(state_file: Path, *, kind: str, signature: str) -> bool:
    if not state_file.is_file():
        return False
    try:
        payload = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict):
        return False
    return payload.get("kind") == kind and payload.get("signature") == signature


def _write_bootstrap_state(state_file: Path, *, kind: str, signature: str) -> None:
    payload = {"kind": kind, "signature": signature}
    state_file.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")


def _clear_bootstrap_state(state_file: Path) -> None:
    try:
        state_file.unlink(missing_ok=True)
    except OSError:
        return


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


def _validate_runtime_root(workspace_dir: str) -> str:
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
    sanitized = _REDACTION_PATTERNS[4].sub(
        lambda m: f"{m.group(1)}[REDACTED]", sanitized
    )
    return sanitized


def _merge_error_summary(existing: str | None, new_error: str) -> str:
    if not existing:
        return new_error
    return f"{existing}; {new_error}"


def _build_run_progress_callback(
    conn: sqlite3.Connection,
    run_id: int,
) -> Callable[[str], None] | None:
    db_path = _resolve_sqlite_database_path(conn)
    if not db_path:
        return None

    def _callback(logs_path: str) -> None:
        try:
            progress_conn = sqlite3.connect(db_path)
            try:
                touch_run_progress(progress_conn, run_id, logs_path=logs_path)
            finally:
                progress_conn.close()
        except sqlite3.Error:
            return None

    return _callback


def _resolve_sqlite_database_path(conn: sqlite3.Connection) -> str | None:
    try:
        row = conn.execute("PRAGMA database_list").fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    if isinstance(row, sqlite3.Row):
        value = row["file"]
    else:
        value = row[2]
    path = _safe_text(value)
    return path or None
