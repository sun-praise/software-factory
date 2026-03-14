from pathlib import Path
import os
from typing import Any
import sqlite3
import httpx
from urllib.parse import urlparse

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
            "status": str(row["status"]),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }
        for row in rows
    ]


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
    if section not in {"pull", "pulls", "issues"}:
        raise ValueError(
            "Only pull request or issue links are supported. "
            "Example: https://github.com/<owner>/<repo>/pull/<number> "
            "or https://github.com/<owner>/<repo>/issues/<number>."
        )

    try:
        pr_number = int(number_part)
    except ValueError as exc:
        raise ValueError("PR number in URL must be a positive integer.") from exc

    if pr_number <= 0:
        raise ValueError("PR number in URL must be a positive integer.")

    repo = f"{owner}/{repo_name}"
    issue_number = None
    if section in {"pull", "pulls"}:
        if section == "pulls":
            section = "pull"
        issue_url = f"https://github.com/{repo}/{section}/{pr_number}"
        return repo, pr_number, issue_number, issue_url

    if section == "issues":
        issue_number = pr_number
        try:
            resolved_pr_number = _resolve_pr_number_from_issue(repo, issue_number)
        except ValueError:
            resolved_pr_number = None
        if resolved_pr_number is None:
            return repo, issue_number, issue_number, f"https://github.com/{repo}/issues/{issue_number}"
        pr_number = resolved_pr_number
        issue_url = f"https://github.com/{repo}/pull/{pr_number}"
        return repo, pr_number, issue_number, issue_url


def _resolve_pr_number_from_issue(repo: str, issue_number: int) -> int | None:
    url = f"https://api.github.com/repos/{repo}/issues/{issue_number}"
    headers = {"User-Agent": "software-factory"}
    token = os.getenv("GITHUB_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        response = httpx.get(url, headers=headers, timeout=10.0)
    except httpx.RequestError as exc:
        raise ValueError(
            "Failed to fetch issue details from GitHub. Please try again."
        ) from exc

    if response.status_code == 404:
        raise ValueError("Issue not found or unavailable.")
    if response.status_code == 403:
        raise ValueError(
            "GitHub API returned 403 while resolving issue. Please retry later or set GITHUB_TOKEN."
        )
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise ValueError("Failed to resolve issue to pull request.") from exc

    try:
        payload = response.json()
    except ValueError as exc:
        raise ValueError("GitHub API returned invalid JSON while resolving issue.") from exc

    if not isinstance(payload, dict):
        raise ValueError("Unexpected GitHub API response while resolving issue.")

    pull_request = payload.get("pull_request")
    if not isinstance(pull_request, dict):
        return None

    return issue_number
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
            SELECT id, status, created_at, updated_at, logs_path
            FROM autofix_runs
            WHERE id = ?
            """,
            (run_id_value,),
        ).fetchone()

    run = {
        "id": run_id,
        "status": "not_found",
        "created_at": "-",
        "updated_at": "-",
        "log_preview": "No log data yet.",
    }
    if row is not None:
        run = {
            "id": str(row["id"]),
            "status": str(row["status"]),
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
    legacy_enabled = "agent_legacy_enabled" in form

    openhands_command = str(form.get("openhands_command", "openhands")).strip()
    openhands_worktree_base_dir = str(
        form.get("openhands_worktree_base_dir", ".software-factory-worktrees")
    ).strip()
    timeout_raw = str(form.get("openhands_command_timeout_seconds", "600"))
    try:
        openhands_command_timeout_seconds = max(1, int(timeout_raw.strip()))
    except (TypeError, ValueError):
        openhands_command_timeout_seconds = 600

    with connect_db() as conn:
        save_agent_feature_flags(
            conn,
            openhands_enabled=openhands_enabled,
            legacy_enabled=legacy_enabled,
            openhands_command=openhands_command,
            openhands_command_timeout_seconds=openhands_command_timeout_seconds,
            openhands_worktree_base_dir=openhands_worktree_base_dir,
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
