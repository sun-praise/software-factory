from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
import re
import shlex
import sqlite3
import subprocess
from typing import Any, Callable, Mapping

from app.services.agent_prompt import (
    build_autofix_prompt,
    collect_check_commands,
    summarize_check_results,
)
from app.services.git_ops import (
    checkout_branch,
    commit_and_push,
    ensure_head_sha,
    post_pr_comment,
)
from app.services.queue import mark_run_finished


Executor = Callable[[str, str], Any]
CHECK_COMMAND_TIMEOUT_SECONDS = 300

_REDACTION_PATTERNS = (
    re.compile(r"(ghp_[A-Za-z0-9]{16,})"),
    re.compile(r"(github_pat_[A-Za-z0-9_]{20,})"),
    re.compile(r"(?i)(token\s*[=:]\s*)([^\s]+)"),
    re.compile(r"(?i)(secret\s*[=:]\s*)([^\s]+)"),
)


@dataclass(frozen=True)
class RunnerOps:
    checkout_branch: Callable[[str, str], tuple[bool, str]] = checkout_branch
    ensure_head_sha: Callable[[str, str], bool] = ensure_head_sha
    commit_and_push: Callable[..., dict[str, Any]] = commit_and_push
    post_pr_comment: Callable[[str, str, int, str], tuple[bool, str]] = post_pr_comment
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
    active_ops = ops or RunnerOps()
    workspace = _validate_workspace_dir(workspace_dir)
    run_id = int(run["id"])
    repo = str(run.get("repo") or "")
    pr_number = int(run.get("pr_number") or 0)

    payload = _parse_payload(run.get("normalized_review_json"))
    head_sha = _safe_text(run.get("head_sha")) or _safe_text(payload.get("head_sha"))
    branch = _resolve_branch(conn, run, payload)
    project_type = _safe_text(payload.get("project_type"))
    commit_message = _safe_text(payload.get("commit_message")) or (
        f"fix: apply autofix updates for PR #{pr_number}"
    )

    prompt = active_ops.build_autofix_prompt(
        repo=repo,
        pr_number=pr_number,
        head_sha=head_sha or "unknown",
        normalized_review=payload,
    )
    commands = active_ops.collect_check_commands(project_type)

    execute = _default_executor if executor is None else executor
    check_results: list[dict[str, Any]] = []
    log_lines = [
        f"run_id={run_id}",
        f"repo={repo}",
        f"pr_number={pr_number}",
        f"head_sha={head_sha or 'unknown'}",
        f"branch={branch or 'unknown'}",
        "prompt:",
        prompt,
        "",
    ]

    for command in commands:
        result = _coerce_result(execute(command, workspace))
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
    status = "failed"
    error_summary: str | None = None
    commit_sha: str | None = None

    if checks_summary["overall_status"] != "passed":
        failed_commands = checks_summary.get("failed_commands") or []
        error_summary = (
            f"checks_failed: {', '.join(str(item) for item in failed_commands)}"
        )
        log_lines.append(error_summary)
    else:
        status, commit_sha, error_summary = _finalize_git_changes(
            repo_dir=workspace,
            branch=branch,
            head_sha=head_sha,
            commit_message=commit_message,
            active_ops=active_ops,
            log_lines=log_lines,
        )

    logs_path = _write_logs(workspace_dir=workspace, run_id=run_id, lines=log_lines)
    mark_run_finished(
        conn=conn,
        run_id=run_id,
        status=status,
        commit_sha=commit_sha,
        error_summary=error_summary,
        logs_path=logs_path,
    )

    comment_body = _build_pr_comment(
        run_id=run_id,
        status=status,
        summary=checks_summary,
        commit_sha=commit_sha,
        error_summary=error_summary,
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
        error_summary = _merge_error_summary(error_summary, comment_failure)
        mark_run_finished(
            conn=conn,
            run_id=run_id,
            status=status,
            commit_sha=commit_sha,
            error_summary=error_summary,
            logs_path=logs_path,
        )

    return {
        "run_id": run_id,
        "status": status,
        "error_summary": error_summary,
        "logs_path": logs_path,
        "commit_sha": commit_sha,
        "checks": checks_summary,
        "comment_posted": posted,
    }


def _finalize_git_changes(
    repo_dir: str,
    branch: str | None,
    head_sha: str | None,
    commit_message: str,
    active_ops: RunnerOps,
    log_lines: list[str],
) -> tuple[str, str | None, str | None]:
    if branch:
        ok, checkout_message = active_ops.checkout_branch(repo_dir, branch)
        log_lines.append(f"checkout: {checkout_message}")
        if not ok:
            return "failed", None, f"checkout_failed: {checkout_message}"

    if head_sha and not active_ops.ensure_head_sha(repo_dir, head_sha):
        log_lines.append("head_sha_check: mismatch")
        return "failed", None, "head_sha_mismatch"

    commit_result = active_ops.commit_and_push(
        repo_dir=repo_dir,
        message=commit_message,
        branch=branch,
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


def _write_logs(workspace_dir: str, run_id: int, lines: list[str]) -> str:
    logs_dir = Path(workspace_dir) / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    logs_file = logs_dir / f"autofix-run-{run_id}.log"
    logs_file.write_text("\n".join(lines), encoding="utf-8")
    return str(logs_file)


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
    for pattern in _REDACTION_PATTERNS:
        if pattern.pattern.startswith("(?i)"):
            sanitized = pattern.sub(lambda m: f"{m.group(1)}[REDACTED]", sanitized)
        else:
            sanitized = pattern.sub("[REDACTED]", sanitized)
    return sanitized


def _merge_error_summary(existing: str | None, new_error: str) -> str:
    if not existing:
        return new_error
    return f"{existing}; {new_error}"
