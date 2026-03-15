import os
import sqlite3
from json import JSONDecodeError
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from app.db import connect_db
from app.config import get_settings
from app.services.github_events import build_review_batch_id, build_task_idempotency_key
from app.services.feature_flags import (
    build_feature_flag_context,
    save_agent_feature_flags,
)
from app.schemas.issues import (
    IssueSubmissionRequest,
)
from app.services.policy import (
    ensure_pull_request_row,
    get_remaining_autofix_quota,
    reset_autofix_count_on_sha_change,
)
from app.services.queue import enqueue_autofix_run


router = APIRouter(tags=["web"])


def _fetch_runs(limit: int = 20) -> list[dict[str, str]]:
    with connect_db() as conn:
        rows = conn.execute(
            """
            SELECT id, repo, pr_number, status, created_at, updated_at
            FROM autofix_runs
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    return [
        {
            "id": str(row["id"]),
            "repo": str(row["repo"]),
            "pr_number": str(row["pr_number"]),
            "pr_url": f"https://github.com/{row['repo']}/pull/{row['pr_number']}",
            "status": str(row["status"]),
            "status_class": _status_class(str(row["status"])),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }
        for row in rows
    ]


def _status_class(status: str) -> str:
    normalized = status.strip().lower()
    if normalized in {"success", "completed"}:
        return "success"
    if normalized in {"failed", "cancelled"}:
        return "failed"
    if normalized in {"running"}:
        return "running"
    if normalized in {"retry_scheduled"}:
        return "retry"
    return "queued"


def _read_log_preview(logs_path: str | None, max_chars: int = 1200) -> str:
    if not logs_path:
        return "No log data yet."
    path = Path(logs_path)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "No log data yet."
    return text.strip()[:max_chars] or "No log data yet."


def _parse_issue_url(url: str) -> tuple[str, int, int | None, str]:
    parsed = urlparse(url)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() != "github.com":
        raise ValueError("Only https GitHub links on github.com are supported.")

    path_parts = [part for part in parsed.path.split("/") if part]
    if len(path_parts) < 4:
        raise ValueError(
            "Expected a GitHub URL in the form https://github.com/<owner>/<repo>/pull/<number> "
            "or https://github.com/<owner>/<repo>/issues/<number>."
        )

    owner, repo_name, section, number_part = path_parts[:4]
    if section in {"pull", "pulls"}:
        try:
            pr_number = int(number_part)
        except ValueError as exc:
            raise ValueError("PR number in URL must be a positive integer.") from exc

        if pr_number <= 0:
            raise ValueError("PR number in URL must be a positive integer.")

        repo = f"{owner}/{repo_name}"
        issue_number = None
        issue_url = f"https://github.com/{repo}/pull/{pr_number}"
        return repo, pr_number, issue_number, issue_url

    if section != "issues":
        raise ValueError(
            "Only pull request or issue links are supported. Example: "
            "https://github.com/<owner>/<repo>/pull/<number> or "
            "https://github.com/<owner>/<repo>/issues/<number>."
        )

    repo = f"{owner}/{repo_name}"
    try:
        issue_number = int(number_part)
    except ValueError as exc:
        raise ValueError("Issue number in URL must be a positive integer.") from exc
    if issue_number <= 0:
        raise ValueError("Issue number in URL must be a positive integer.")

    resolved_pr_number = _resolve_pr_number_from_issue(
        owner=owner,
        repo_name=repo_name,
        issue_number=issue_number,
    )
    pr_number = resolved_pr_number or issue_number
    if resolved_pr_number is None:
        issue_url = f"https://github.com/{repo}/issues/{issue_number}"
    else:
        issue_url = f"https://github.com/{repo}/pull/{pr_number}"

    return repo, pr_number, issue_number, issue_url


def _resolve_pr_number_from_issue(
    *,
    owner: str,
    repo_name: str,
    issue_number: int,
) -> int | None:
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "software-factory",
    }
    if token:
        headers["Authorization"] = f"token {token}"
    url = f"https://api.github.com/repos/{owner}/{repo_name}/issues/{issue_number}"
    try:
        response = httpx.get(url, headers=headers, timeout=10.0)
    except httpx.RequestError as exc:
        raise ValueError(f"Failed to query issue details: {exc}") from exc

    if response.status_code == 404:
        raise ValueError("Issue not found or unavailable.")
    if response.status_code == 403:
        raise ValueError("GitHub API access denied while resolving issue details.")
    if response.status_code == 401:
        raise ValueError("Unauthorized when querying GitHub issue details.")
    if response.status_code >= 400:
        raise ValueError(
            f"GitHub API returned unexpected status: {response.status_code}."
        )

    try:
        payload = response.json()
    except JSONDecodeError as exc:
        raise ValueError("GitHub API returned invalid JSON.") from exc
    if not isinstance(payload, dict):
        raise ValueError("Unexpected response from GitHub API.")

    pull_request_info = payload.get("pull_request")
    if not isinstance(pull_request_info, dict):
        return None

    pr_url = pull_request_info.get("url", "")
    if not isinstance(pr_url, str):
        return None

    pull_url_parts = [part for part in pr_url.split("/") if part]
    try:
        return int(pull_url_parts[-1])
    except (TypeError, ValueError):
        return None


def _build_issue_normalized_review(
    *,
    repo: str,
    pr_number: int,
    issue_number: int | None,
    issue_url: str,
) -> dict[str, Any]:
    issue_text = f"Manual issue submission: {issue_url}"
    if issue_number is not None:
        issue_text = f"{issue_text}\n\nOriginal issue number: {issue_number}"

    item = {
        "source": "manual_issue",
        "path": None,
        "line": None,
        "text": issue_text,
        "severity": "P1",
    }

    must_fix: list[dict[str, Any]] = [item]
    should_fix: list[dict[str, Any]] = []

    return {
        "repo": repo,
        "pr_number": pr_number,
        "head_sha": None,
        "must_fix": must_fix,
        "should_fix": should_fix,
        "ignore": [],
        "summary": f"{len(must_fix)} blocking issues, {len(should_fix)} suggestions, 0 ignored",
        "project_type": "python",
        "issue_number": issue_number,
    }


def _enqueue_issue_fix(
    *,
    repo: str,
    pr_number: int,
    issue_number: int | None,
    issue_url: str,
) -> dict[str, Any]:
    run_id: int | None = None
    remaining_quota = None
    idempotency_key = None
    queue_status = "not_queued"

    normalized_review = _build_issue_normalized_review(
        repo=repo,
        pr_number=pr_number,
        issue_number=issue_number,
        issue_url=issue_url,
    )
    head_sha = None

    review_batch_id = build_review_batch_id(normalized_review)
    normalized_review["review_batch_id"] = review_batch_id
    idempotency_key = build_task_idempotency_key(
        repo=repo,
        pr_number=pr_number,
        head_sha=head_sha,
        review_batch_id=review_batch_id,
    )

    with connect_db() as conn:
        if head_sha:
            reset_autofix_count_on_sha_change(
                conn,
                repo,
                pr_number,
                head_sha,
            )
        ensure_pull_request_row(
            conn,
            repo,
            pr_number,
            branch=None,
            head_sha=head_sha,
        )
        remaining_quota = get_remaining_autofix_quota(conn, repo, pr_number)
        if remaining_quota == 0:
            queue_status = "autofix_limit_reached"
        else:
            run_id = enqueue_autofix_run(
                conn=conn,
                repo=repo,
                pr_number=pr_number,
                head_sha=head_sha,
                normalized_review_json=normalized_review,
                trigger_source="manual_issue",
                idempotency_key=idempotency_key,
                max_attempts=get_settings().max_retry_attempts,
            )
            queue_status = "queued" if run_id is not None else "duplicate_task"

    return {
        "ok": True,
        "message": "Issue submission accepted.",
        "repo": repo,
        "pr_number": pr_number,
        "issue_number": issue_number,
        "queue_status": queue_status,
        "queued_run_id": run_id,
        "idempotency_key": idempotency_key,
        "remaining_quota": remaining_quota,
        "head_sha": head_sha,
    }


@router.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    templates: Jinja2Templates = request.app.state.templates
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "request": request,
            "title": "Software Factory",
            "runs": _fetch_runs(),
        },
    )


@router.get("/runs", response_class=HTMLResponse)
async def runs_page(request: Request) -> HTMLResponse:
    return await index(request)


@router.get("/runs/{run_id}", response_class=HTMLResponse)
async def run_detail(request: Request, run_id: str) -> HTMLResponse:
    templates: Jinja2Templates = request.app.state.templates
    try:
        run_id_value = int(run_id)
    except ValueError:
        run_id_value = -1

    with connect_db() as conn:
        row = conn.execute(
            """
            SELECT id, repo, pr_number, status, created_at, updated_at, logs_path
            FROM autofix_runs
            WHERE id = ?
            """,
            (run_id_value,),
        ).fetchone()

    run = {
        "id": run_id,
        "repo": "-",
        "pr_number": "-",
        "pr_url": None,
        "status": "not_found",
        "status_class": _status_class("not_found"),
        "created_at": "-",
        "updated_at": "-",
        "log_preview": "No log data yet.",
    }
    if row is not None:
        repo = str(row["repo"])
        pr_number = str(row["pr_number"])
        run = {
            "id": str(row["id"]),
            "repo": repo,
            "pr_number": pr_number,
            "pr_url": f"https://github.com/{repo}/pull/{pr_number}",
            "status": str(row["status"]),
            "status_class": _status_class(str(row["status"])),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
            "log_preview": _read_log_preview(row["logs_path"]),
        }
    return templates.TemplateResponse(
        request=request,
        name="run_detail.html",
        context={"request": request, "run": run},
    )


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request) -> HTMLResponse:
    templates: Jinja2Templates = request.app.state.templates
    with connect_db() as conn:
        flag_context = build_feature_flag_context(conn)
    return templates.TemplateResponse(
        request=request,
        name="settings.html",
        context={
            "request": request,
            "title": "Software Factory - Settings",
            "saved": request.query_params.get("saved") == "1",
            **flag_context,
        },
    )


@router.post("/settings", response_class=HTMLResponse)
async def save_settings(request: Request) -> RedirectResponse:
    form = await request.form()
    openhands_enabled = "agent_openhands_enabled" in form
    claude_agent_enabled = "agent_claude_agent_enabled" in form

    openhands_command = str(form.get("openhands_command", "openhands")).strip()
    claude_agent_command = str(form.get("claude_agent_command", "claude")).strip()
    openhands_worktree_base_dir = str(
        form.get("openhands_worktree_base_dir", ".software-factory-worktrees")
    ).strip()
    claude_agent_worktree_base_dir = str(
        form.get("claude_agent_worktree_base_dir", ".software-factory-worktrees")
    ).strip()
    timeout_raw = str(form.get("openhands_command_timeout_seconds", "600"))
    try:
        openhands_command_timeout_seconds = max(1, int(timeout_raw.strip()))
    except (TypeError, ValueError):
        openhands_command_timeout_seconds = 600
    claude_timeout_raw = str(
        form.get("claude_agent_command_timeout_seconds", "600")
    ).strip()
    try:
        claude_agent_command_timeout_seconds = max(1, int(claude_timeout_raw))
    except (TypeError, ValueError):
        claude_agent_command_timeout_seconds = 600

    with connect_db() as conn:
        save_agent_feature_flags(
            conn,
            openhands_enabled=openhands_enabled,
            claude_agent_enabled=claude_agent_enabled,
            openhands_command=openhands_command,
            openhands_command_timeout_seconds=openhands_command_timeout_seconds,
            openhands_worktree_base_dir=openhands_worktree_base_dir,
            claude_agent_command=claude_agent_command,
            claude_agent_command_timeout_seconds=(
                claude_agent_command_timeout_seconds
            ),
            claude_agent_worktree_base_dir=claude_agent_worktree_base_dir,
        )

    return RedirectResponse(url="/settings?saved=1", status_code=303)


@router.get("/issues", response_class=HTMLResponse)
async def issue_entry_page(request: Request) -> HTMLResponse:
    templates: Jinja2Templates = request.app.state.templates
    return templates.TemplateResponse(
        request=request,
        name="issue_submit.html",
        context={
            "request": request,
            "title": "Submit Manual Issue",
            "message": None,
            "result": None,
            "form": {},
        },
    )


@router.post("/issues", response_class=HTMLResponse)
async def submit_issue(request: Request) -> HTMLResponse:
    templates: Jinja2Templates = request.app.state.templates
    form = await request.form()
    request_data = {"url": str(form.get("url", "")).strip()}

    try:
        payload = IssueSubmissionRequest.model_validate(request_data)
    except (TypeError, ValueError, ValidationError):
        return templates.TemplateResponse(
            request=request,
            name="issue_submit.html",
            context={
                "request": request,
                "title": "Submit Manual Issue",
                "message": "Invalid input. Please check required fields.",
                "result": None,
                "form": request_data,
            },
            status_code=400,
        )

    try:
        repo, pr_number, issue_number, issue_url = _parse_issue_url(payload.url)
    except ValueError as exc:
        return templates.TemplateResponse(
            request=request,
            name="issue_submit.html",
            context={
                "request": request,
                "title": "Submit Manual Issue",
                "message": str(exc),
                "result": None,
                "form": request_data,
            },
            status_code=400,
        )

    try:
        result = _enqueue_issue_fix(
            repo=repo,
            pr_number=pr_number,
            issue_number=issue_number,
            issue_url=issue_url,
        )
    except sqlite3.Error:
        result = {
            "ok": False,
            "message": "Failed to enqueue issue-based autofix",
        }

    return templates.TemplateResponse(
        request=request,
        name="issue_submit.html",
        context={
            "request": request,
            "title": "Submit Manual Issue",
            "message": "Submitted",
            "result": result,
            "form": request_data,
        },
    )


@router.post("/api/issues")
async def api_submit_issue(payload: IssueSubmissionRequest) -> dict[str, Any]:
    try:
        repo, pr_number, issue_number, issue_url = _parse_issue_url(payload.url)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    try:
        return _enqueue_issue_fix(
            repo=repo,
            pr_number=pr_number,
            issue_number=issue_number,
            issue_url=issue_url,
        )
    except sqlite3.Error as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "ok": False,
                "message": "Failed to enqueue issue-based autofix",
                "error": str(exc),
            },
        ) from exc
