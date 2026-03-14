from pathlib import Path
from typing import Any
import sqlite3

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


def _build_issue_normalized_review(payload: IssueSubmissionRequest) -> dict[str, Any]:
    issue_text = f"{payload.title}\n\n{payload.body}".strip()
    item = {
        "source": "manual_issue",
        "path": None,
        "line": None,
        "text": issue_text,
        "severity": payload.severity,
    }

    must_fix: list[dict[str, Any]] = []
    should_fix: list[dict[str, Any]] = []
    if payload.priority == "must_fix":
        must_fix.append(item)
    else:
        should_fix.append(item)

    return {
        "repo": payload.repo,
        "pr_number": payload.pr_number,
        "head_sha": payload.head_sha,
        "must_fix": must_fix,
        "should_fix": should_fix,
        "ignore": [],
        "summary": f"{len(must_fix)} blocking issues, {len(should_fix)} suggestions, 0 ignored",
        "project_type": payload.project_type or "python",
        "issue_number": payload.issue_number,
    }


def _enqueue_issue_fix(payload: IssueSubmissionRequest) -> dict[str, Any]:
    run_id: int | None = None
    remaining_quota = None
    idempotency_key = None
    queue_status = "not_queued"

    normalized_review = _build_issue_normalized_review(payload)
    repo = payload.repo
    pr_number = payload.pr_number
    branch = payload.branch
    head_sha = payload.head_sha

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
            branch=branch,
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
        "issue_number": payload.issue_number,
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

    raw_issue_number = form.get("issue_number")
    issue_number: int | None = None
    if raw_issue_number not in (None, ""):
        try:
            issue_number = int(str(raw_issue_number))
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="issue_number must be a positive integer",
            ) from exc

    request_data = {
        "repo": form.get("repo", "").strip(),
        "pr_number": form.get("pr_number"),
        "issue_number": issue_number,
        "title": form.get("title", "").strip(),
        "body": form.get("body", "").strip(),
        "head_sha": (form.get("head_sha") or "").strip() or None,
        "branch": (form.get("branch") or "").strip() or None,
        "priority": form.get("priority", "must_fix"),
        "severity": form.get("severity", "P1"),
        "project_type": (form.get("project_type") or "").strip() or None,
    }

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
        result = _enqueue_issue_fix(payload)
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
        return _enqueue_issue_fix(payload)
    except sqlite3.Error as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "ok": False,
                "message": "Failed to enqueue issue-based autofix",
                "error": str(exc),
            },
        ) from exc
