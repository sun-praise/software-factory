import csv
import io
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from app.db import connect_db
from app.providers import get_git_remote_provider, get_task_source_provider
from app.services.github_events import build_review_batch_id, build_task_idempotency_key
from app.services.feature_flags import (
    AgentFeatureFlags,
    build_selected_agent_sdks,
    build_feature_flag_context,
    save_agent_feature_flags,
)
from app.services.runtime_settings import (
    RuntimeSettingsPayload,
    build_runtime_settings_context,
    describe_runtime_settings,
    get_runtime_form_int_field_specs,
    parse_settings_list_form_value,
    resolve_runtime_settings,
    save_runtime_settings,
)
from app.schemas.issues import (
    IssueSubmissionRequest,
)
from app.services.policy import (
    ensure_pull_request_row,
    get_remaining_autofix_quota,
    reset_autofix_count_on_sha_change,
)
from app.services.queue import (
    append_run_operator_hint,
    enqueue_autofix_run,
    request_run_cancel,
)
from app.services.run_hints import RUN_HINT_EDITABLE_STATUSES
from app.services.task_source import (
    TEXT_SOURCE_KIND,
    build_manual_text_task_number,
    coerce_positive_int,
)


_ACTIVE_RUN_STATUSES = {"queued", "running", "cancel_requested", "retry_scheduled"}

_TRUE_VALUES = frozenset({"true", "1", "yes", "on"})

_RUNTIME_INT_FIELD_SPECS = get_runtime_form_int_field_specs()


def _parse_bool_like(value: str | None) -> bool:
    if value is None:
        return False
    return str(value).strip().lower() in _TRUE_VALUES


def _escape_like_pattern(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _find_existing_run_by_source_url(
    conn: sqlite3.Connection,
    source_url: str,
) -> dict[str, Any] | None:
    escaped_url = _escape_like_pattern(source_url)
    cursor = conn.execute(
        """
        SELECT id, status, normalized_review_json
        FROM autofix_runs
        WHERE trigger_source = 'manual_issue'
          AND normalized_review_json LIKE ? ESCAPE '\\'
        ORDER BY id DESC
        LIMIT 10
        """,
        (f"%{escaped_url}%",),
    )
    rows = cursor.fetchall()
    for row in rows:
        try:
            review_json = json.loads(row["normalized_review_json"] or "{}")
        except json.JSONDecodeError:
            continue
        if review_json.get("manual_issue_source_url") == source_url:
            return {
                "id": row["id"],
                "status": row["status"],
            }
    return None


router = APIRouter(tags=["web"])


@dataclass(frozen=True)
class ParsedTaskTarget:
    repo: str
    owner: str
    repo_name: str
    pr_number: int
    resolved_pr_number: int | None
    issue_number: int | None
    source_ref: str
    source_fragment: str
    source_kind: str
    task_title: str | None = None
    task_text: str | None = None


@dataclass(frozen=True)
class ManualIssueContext:
    text: str
    path: str | None = None
    line: int | None = None
    source_url: str | None = None


def _normalize_page(raw_value: str | None, *, default: int = 1) -> int:
    try:
        value = int((raw_value or "").strip())
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _fetch_runs(
    *,
    page: int = 1,
    page_size: int = 10,
    query: str = "",
) -> dict[str, Any]:
    normalized_query = query.strip()
    sql_where = ""
    sql_params: list[Any] = []
    if normalized_query:
        like_value = f"%{normalized_query.lower()}%"
        sql_where = """
            WHERE
                lower(repo) LIKE ?
                OR lower(status) LIKE ?
                OR CAST(id AS TEXT) LIKE ?
                OR CAST(pr_number AS TEXT) LIKE ?
        """
        sql_params.extend([like_value, like_value, like_value, like_value])

    offset = (page - 1) * page_size
    with connect_db() as conn:
        count_row = conn.execute(
            f"""
            SELECT COUNT(*) AS total_count
            FROM autofix_runs
            {sql_where}
            """,
            tuple(sql_params),
        ).fetchone()
        rows = conn.execute(
            f"""
            SELECT id, repo, pr_number, opened_pr_number, opened_pr_url, trigger_source, status, created_at, updated_at, normalized_review_json
            FROM autofix_runs
            {sql_where}
            ORDER BY id DESC
            LIMIT ? OFFSET ?
            """,
            tuple([*sql_params, page_size, offset]),
        ).fetchall()

    total_count = int(count_row["total_count"]) if count_row is not None else 0
    total_pages = max(1, (total_count + page_size - 1) // page_size)
    normalized_page = min(page, total_pages)
    runs = [
        {
            "id": str(row["id"]),
            "repo": str(row["repo"]) if row["repo"] is not None else "-",
            "pr_number": _resolve_run_pr_number(row),
            "pr_url": _resolve_run_pr_url(row),
            "trigger_source": row["trigger_source"],
            "issue_number": issue_meta["issue_number"],
            "issue_url": issue_meta["issue_url"],
            "status": str(row["status"]),
            "status_class": _status_class(str(row["status"])),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }
        for row in rows
        for issue_meta in [_extract_issue_metadata(row)]
    ]
    return {
        "items": runs,
        "page": normalized_page,
        "page_size": page_size,
        "total_count": total_count,
        "total_pages": total_pages,
        "query": normalized_query,
        "has_prev": normalized_page > 1,
        "has_next": normalized_page < total_pages,
        "prev_page": normalized_page - 1,
        "next_page": normalized_page + 1,
    }


def _status_class(status: str) -> str:
    normalized = status.strip().lower()
    if normalized in {"success", "completed"}:
        return "success"
    if normalized in {"failed", "cancelled"}:
        return "failed"
    if normalized in {"running", "cancel_requested"}:
        return "running"
    if normalized in {"retry_scheduled"}:
        return "retry"
    return "queued"


def _extract_issue_metadata(row: sqlite3.Row) -> dict[str, str]:
    trigger = _string_or_empty(row["trigger_source"])
    if trigger != "manual_issue":
        return {"trigger_source": trigger or "pr", "issue_number": "", "issue_url": ""}
    try:
        review_json = json.loads(row["normalized_review_json"] or "{}")
    except (json.JSONDecodeError, TypeError):
        return {"trigger_source": "manual_issue", "issue_number": "", "issue_url": ""}
    issue_number = coerce_positive_int(review_json.get("issue_number"))
    source_url = _string_or_empty(review_json.get("manual_issue_source_url"))
    return {
        "trigger_source": "manual_issue",
        "issue_number": str(issue_number) if issue_number else "",
        "issue_url": source_url,
    }


def _resolve_run_pr_number(row: sqlite3.Row) -> str:
    opened_pr_number = coerce_positive_int(row["opened_pr_number"])
    if opened_pr_number is not None:
        return str(opened_pr_number)
    if _string_or_empty(row["trigger_source"]) == "manual_task":
        return "-"
    return str(row["pr_number"]) if row["pr_number"] is not None else "-"


def _resolve_run_pr_url(row: sqlite3.Row) -> str:
    opened_pr_url = _string_or_empty(row["opened_pr_url"])
    if opened_pr_url:
        return opened_pr_url
    repo = _string_or_empty(row["repo"])
    pr_number = coerce_positive_int(row["pr_number"])
    if not repo or pr_number is None:
        return ""
    if _string_or_empty(row["trigger_source"]) in {"manual_issue", "manual_task"}:
        return ""
    provider = get_git_remote_provider()
    return provider.build_pull_request_url(repo=repo, pr_number=pr_number)


def _read_log_preview(logs_path: str | None, max_chars: int = 1200) -> str:
    if not logs_path:
        return "No log data yet."
    path = Path(logs_path)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "No log data yet."
    return text.strip()[:max_chars] or "No log data yet."


def _read_run_log(logs_path: str | None, max_chars: int = 24000) -> str:
    if not logs_path:
        return "No log data yet."
    path = Path(logs_path)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "No log data yet."
    trimmed = text.strip()
    if not trimmed:
        return "No log data yet."
    if len(trimmed) <= max_chars:
        return trimmed
    return trimmed[-max_chars:]


def _load_run_detail(run_id_value: int) -> dict[str, str]:
    with connect_db() as conn:
        row = conn.execute(
            """
            SELECT id, repo, pr_number, opened_pr_number, opened_pr_url, trigger_source, status, created_at, updated_at, logs_path, operator_hints, normalized_review_json
            FROM autofix_runs
            WHERE id = ?
            """,
            (run_id_value,),
        ).fetchone()

    if row is None:
        return {
            "id": str(run_id_value),
            "repo": "-",
            "pr_number": "-",
            "pr_url": "",
            "trigger_source": "",
            "issue_number": "",
            "issue_url": "",
            "status": "not_found",
            "created_at": "-",
            "updated_at": "-",
            "log_preview": "No log data yet.",
            "operator_hints": "",
            "operator_hints_editable": "false",
        }

    repo = str(row["repo"]) if row["repo"] is not None else "-"
    pr_number = _resolve_run_pr_number(row)
    pr_url = _resolve_run_pr_url(row)
    issue_meta = _extract_issue_metadata(row)
    return {
        "id": str(row["id"]),
        "repo": repo,
        "pr_number": pr_number,
        "pr_url": pr_url,
        "trigger_source": issue_meta["trigger_source"],
        "issue_number": issue_meta["issue_number"],
        "issue_url": issue_meta["issue_url"],
        "status": str(row["status"]),
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
        "log_preview": _read_run_log(row["logs_path"]),
        "operator_hints": str(row["operator_hints"] or ""),
        "operator_hints_editable": (
            "true" if str(row["status"]) in RUN_HINT_EDITABLE_STATUSES else "false"
        ),
    }


def _parse_task_submission(payload: IssueSubmissionRequest) -> ParsedTaskTarget:
    provider = get_task_source_provider()
    parsed_submission = provider.parse_task_submission(
        submission=payload.model_dump(mode="python")
    )
    if not isinstance(parsed_submission, Mapping):
        raise ValueError("Unexpected response from task-source provider.")

    parsed = dict(parsed_submission)
    repo = _string_or_empty(parsed.get("repo"))
    owner = _string_or_empty(parsed.get("owner"))
    repo_name = _string_or_empty(parsed.get("repo_name"))

    if not repo and owner and repo_name:
        repo = f"{owner}/{repo_name}"
    if not repo:
        raise ValueError("Repository must be in the form <owner>/<repo>.")

    if not owner or not repo_name:
        repo_parts = [part for part in repo.split("/") if part]
        if len(repo_parts) != 2:
            raise ValueError("Repository must be in the form <owner>/<repo>.")
        owner, repo_name = repo_parts
        repo = f"{owner}/{repo_name}"

    source_kind = _string_or_empty(parsed.get("source_kind")).lower()
    if not source_kind:
        raise ValueError("Task source kind is required.")

    issue_number = coerce_positive_int(parsed.get("issue_number"))
    resolved_pr_number = coerce_positive_int(parsed.get("resolved_pr_number"))
    pr_number = coerce_positive_int(parsed.get("pr_number"))
    source_ref = _string_or_empty(parsed.get("source_ref"))
    source_fragment = _string_or_empty(parsed.get("source_fragment"))
    task_title = _string_or_empty(parsed.get("task_title")) or None
    task_text = _string_or_empty(parsed.get("task_text")) or None

    if (
        source_kind == "issue"
        and issue_number is not None
        and resolved_pr_number is None
    ):
        resolved_pr_number = _resolve_pr_number_from_issue(
            owner=owner,
            repo_name=repo_name,
            issue_number=issue_number,
        )

    if source_kind == "pull":
        if pr_number is None:
            raise ValueError("PR number in URL must be a positive integer.")
        if resolved_pr_number is None:
            resolved_pr_number = pr_number
    elif source_kind == "issue":
        if issue_number is None:
            issue_number = pr_number
        if issue_number is None:
            raise ValueError("Issue number in URL must be a positive integer.")
        pr_number = resolved_pr_number or issue_number
    elif source_kind == TEXT_SOURCE_KIND:
        if not task_text:
            raise ValueError("Task text is required for non-GitHub submissions.")
        if pr_number is None:
            pr_number = build_manual_text_task_number(
                repo=repo,
                text=task_text,
                title=task_title,
            )
        resolved_pr_number = None
        issue_number = None
        source_ref = ""
        source_fragment = ""

    if pr_number is None:
        raise ValueError("Could not resolve a task identifier from submission.")

    return ParsedTaskTarget(
        repo=repo,
        owner=owner,
        repo_name=repo_name,
        pr_number=pr_number,
        resolved_pr_number=resolved_pr_number,
        issue_number=issue_number,
        source_ref=source_ref,
        source_fragment=source_fragment,
        source_kind=source_kind,
        task_title=task_title,
        task_text=task_text,
    )


def _string_or_empty(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _resolve_manual_issue_context(
    target: ParsedTaskTarget,
    *,
    description_present: bool,
) -> ManualIssueContext | None:
    provider = get_task_source_provider()
    raw_context = provider.resolve_manual_issue_context(
        repo=target.repo,
        pr_number=target.pr_number,
        issue_number=target.issue_number,
        source_kind=target.source_kind,
        source_ref=target.source_ref,
        source_fragment=target.source_fragment,
        description_present=description_present,
    )
    if raw_context is None:
        return None
    if not isinstance(raw_context, Mapping):
        raise ValueError("Unexpected response from task-source provider.")

    context_text = _string_or_empty(raw_context.get("text"))
    if not context_text:
        if description_present:
            return None
        raise ValueError("Manual issue context is empty.")

    return ManualIssueContext(
        text=context_text,
        path=_string_or_empty(raw_context.get("path")) or None,
        line=coerce_positive_int(raw_context.get("line")),
        source_url=_string_or_empty(raw_context.get("source_url")) or target.source_ref,
    )


def _fetch_pull_request_feedback_review(
    target: ParsedTaskTarget,
    *,
    project_root: str | None = None,
) -> dict[str, Any]:
    provider = get_task_source_provider()
    raw_normalized = provider.fetch_pull_request_feedback_review(
        repo=target.repo,
        pr_number=target.pr_number,
    )
    if not isinstance(raw_normalized, dict):
        raise ValueError("Unexpected response from task-source provider.")
    normalized = dict(raw_normalized)
    normalized["project_type"] = "python"
    normalized["project_root"] = project_root
    normalized["source_kind"] = target.source_kind
    normalized["resolved_pr_number"] = target.resolved_pr_number
    normalized["manual_issue_source_url"] = target.source_ref
    normalized["issue_number"] = target.issue_number
    return normalized


def _resolve_pr_number_from_issue(
    *,
    owner: str,
    repo_name: str,
    issue_number: int,
) -> int | None:
    provider = get_task_source_provider()
    return provider.resolve_pull_request_number_from_issue(
        repo=f"{owner}/{repo_name}",
        issue_number=issue_number,
    )


def _build_issue_normalized_review(
    *,
    target: ParsedTaskTarget,
    description: str | None,
    resolved_context: ManualIssueContext | None,
    project_root: str | None = None,
) -> dict[str, Any]:
    issue_parts = [f"Manual issue submission: {target.source_ref}"]
    if target.issue_number is not None:
        issue_parts.append(f"Original issue number: {target.issue_number}")
    if description:
        issue_parts.append(f"Operator note:\n{description}")
    if resolved_context is not None:
        context_source = resolved_context.source_url or target.source_ref
        issue_parts.append(f"GitHub context source: {context_source}")
        issue_parts.append(f"GitHub context:\n{resolved_context.text}")

    issue_text = "\n\n".join(part for part in issue_parts if part)
    context_resolved = bool(description or resolved_context is not None)

    item = {
        "source": "manual_issue",
        "path": resolved_context.path if resolved_context is not None else None,
        "line": resolved_context.line if resolved_context is not None else None,
        "text": issue_text,
        "severity": "P1",
        "source_url": target.source_ref,
        "context_resolved": context_resolved,
    }

    must_fix: list[dict[str, Any]] = [item]
    should_fix: list[dict[str, Any]] = []

    return {
        "repo": target.repo,
        "pr_number": target.pr_number,
        "head_sha": None,
        "must_fix": must_fix,
        "should_fix": should_fix,
        "ignore": [],
        "summary": f"{len(must_fix)} blocking issues, {len(should_fix)} suggestions, 0 ignored",
        "project_type": "python",
        "project_root": project_root,
        "source_kind": target.source_kind,
        "resolved_pr_number": target.resolved_pr_number,
        "issue_number": target.issue_number,
        "manual_issue_source_url": target.source_ref,
    }


def _build_text_normalized_review(
    *,
    target: ParsedTaskTarget,
    description: str | None,
) -> dict[str, Any]:
    task_parts = []
    if target.task_title:
        task_parts.append(f"Title: {target.task_title}")
    task_parts.append(target.task_text or "")
    if description:
        task_parts.append(f"Operator note:\n{description}")
    task_text = "\n\n".join(part for part in task_parts if part)
    must_fix = [
        {
            "source": "manual_text",
            "path": None,
            "line": None,
            "text": f"Manual task input\n\n{task_text}",
            "severity": "P1",
            "context_resolved": True,
        }
    ]
    return {
        "repo": target.repo,
        "pr_number": target.pr_number,
        "head_sha": None,
        "must_fix": must_fix,
        "should_fix": [],
        "ignore": [],
        "summary": "1 blocking issues, 0 suggestions, 0 ignored",
        "project_type": "python",
        "source_kind": target.source_kind,
        "resolved_pr_number": None,
        "issue_number": None,
        "task_title": target.task_title,
        "task_text": target.task_text,
    }


def _enqueue_task_fix(
    *,
    target: ParsedTaskTarget,
    description: str | None,
    resolved_context: ManualIssueContext | None,
    dry_run: bool = False,
    project_root: str | None = None,
) -> dict[str, Any]:
    run_id: int | None = None
    existing_run_id: int | None = None
    existing_run_status: str | None = None
    remaining_quota = None
    idempotency_key = None
    queue_status = "not_queued"

    if (
        target.source_kind == "pull"
        and not target.source_fragment
        and description is None
    ):
        normalized_review = _fetch_pull_request_feedback_review(
            target, project_root=project_root
        )
        if not normalized_review.get("must_fix") and not normalized_review.get(
            "should_fix"
        ):
            raise ValueError(
                "No actionable pull request comments were found. Provide a specific comment link or a manual issue description."
            )
    elif target.source_kind == TEXT_SOURCE_KIND:
        normalized_review = _build_text_normalized_review(
            target=target,
            description=description,
        )
    else:
        if resolved_context is None and description is None:
            raise ValueError(
                "Please provide a specific GitHub comment/issue link or add a description."
            )
        normalized_review = _build_issue_normalized_review(
            target=target,
            description=description,
            resolved_context=resolved_context,
            project_root=project_root,
        )
    normalized_review.setdefault("source_kind", target.source_kind)
    normalized_review.setdefault("resolved_pr_number", target.resolved_pr_number)
    normalized_review.setdefault("issue_number", target.issue_number)
    normalized_review.setdefault("manual_issue_source_url", target.source_ref)
    normalized_review.setdefault("task_title", target.task_title)
    normalized_review.setdefault("task_text", target.task_text)
    head_sha = None

    review_batch_id = build_review_batch_id(normalized_review)
    normalized_review["review_batch_id"] = review_batch_id
    idempotency_key = build_task_idempotency_key(
        repo=target.repo,
        pr_number=target.pr_number,
        head_sha=head_sha,
        review_batch_id=review_batch_id,
    )

    if dry_run:
        return {
            "ok": True,
            "message": "Task validation successful (dry run - no run created).",
            "repo": target.repo,
            "pr_number": target.pr_number,
            "issue_number": target.issue_number,
            "source_kind": target.source_kind,
            "source_ref": target.source_ref or None,
            "queue_status": "validated",
            "queued_run_id": None,
            "idempotency_key": idempotency_key,
            "remaining_quota": None,
            "head_sha": head_sha,
            "existing_run_id": None,
            "existing_run_status": None,
        }

    source_url = normalized_review.get("manual_issue_source_url")

    final_idempotency_key: str | None = idempotency_key

    with connect_db() as conn:
        runtime_settings = resolve_runtime_settings(conn)
        if source_url and target.source_kind == "issue":
            existing_run = _find_existing_run_by_source_url(conn, source_url)
            if existing_run is not None:
                existing_run_id = existing_run["id"]
                existing_run_status = existing_run["status"]
                if existing_run_status in _ACTIVE_RUN_STATUSES:
                    run_row = conn.execute(
                        "SELECT id FROM autofix_runs WHERE id = ?",
                        (existing_run_id,),
                    ).fetchone()
                    if run_row is not None:
                        return {
                            "ok": True,
                            "message": "Found existing active run for this issue.",
                            "repo": target.repo,
                            "pr_number": target.pr_number,
                            "issue_number": target.issue_number,
                            "source_kind": target.source_kind,
                            "source_ref": target.source_ref or None,
                            "queue_status": "reused_active_run",
                            "queued_run_id": existing_run_id,
                            "idempotency_key": idempotency_key,
                            "remaining_quota": None,
                            "head_sha": head_sha,
                            "existing_run_id": existing_run_id,
                            "existing_run_status": existing_run_status,
                        }
                else:
                    final_idempotency_key = None

        if head_sha:
            reset_autofix_count_on_sha_change(
                conn,
                target.repo,
                target.pr_number,
                head_sha,
            )
        ensure_pull_request_row(
            conn,
            target.repo,
            target.pr_number,
            branch=None,
            head_sha=head_sha,
        )
        remaining_quota = get_remaining_autofix_quota(
            conn,
            target.repo,
            target.pr_number,
            max_autofix_per_pr=runtime_settings.max_autofix_per_pr,
        )
        if remaining_quota == 0:
            queue_status = "autofix_limit_reached"
        else:
            run_id = enqueue_autofix_run(
                conn=conn,
                repo=target.repo,
                pr_number=target.pr_number,
                head_sha=head_sha,
                normalized_review_json=normalized_review,
                trigger_source=(
                    "manual_task"
                    if target.source_kind == TEXT_SOURCE_KIND
                    else "manual_issue"
                ),
                idempotency_key=final_idempotency_key,
                max_attempts=runtime_settings.max_retry_attempts,
            )
            queue_status = "queued" if run_id is not None else "duplicate_task"

    return {
        "ok": True,
        "message": "Task submission accepted.",
        "repo": target.repo,
        "pr_number": target.pr_number,
        "issue_number": target.issue_number,
        "source_kind": target.source_kind,
        "source_ref": target.source_ref or None,
        "queue_status": queue_status,
        "queued_run_id": run_id,
        "idempotency_key": idempotency_key,
        "remaining_quota": remaining_quota,
        "head_sha": head_sha,
        "existing_run_id": existing_run_id,
        "existing_run_status": existing_run_status,
    }


@router.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    templates: Jinja2Templates = request.app.state.templates
    query = str(request.query_params.get("q", "")).strip()
    page = _normalize_page(request.query_params.get("page"))
    run_page = _fetch_runs(page=page, page_size=10, query=query)
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "request": request,
            "title": "Software Factory",
            "runs": run_page["items"],
            "run_page": run_page,
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
    run = _load_run_detail(run_id_value)
    return templates.TemplateResponse(
        request=request,
        name="run_detail.html",
        context={
            "request": request,
            "run": run,
            "hint_editable_statuses": sorted(RUN_HINT_EDITABLE_STATUSES),
        },
    )


@router.get("/api/runs/{run_id}")
async def api_run_detail(run_id: str) -> JSONResponse:
    try:
        run_id_value = int(run_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="run_id must be an integer",
        )
    return JSONResponse(_load_run_detail(run_id_value))


@router.post("/api/runs/{run_id}/cancel")
async def api_cancel_run(run_id: str) -> JSONResponse:
    try:
        run_id_value = int(run_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="run_id must be an integer",
        )

    with connect_db() as conn:
        cancelled_status = request_run_cancel(conn, run_id_value)

    if cancelled_status is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="run not found",
        )
    return JSONResponse(_load_run_detail(run_id_value))


@router.post("/api/runs/{run_id}/operator-hints")
async def api_append_run_operator_hints(run_id: str, request: Request) -> JSONResponse:
    try:
        run_id_value = int(run_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="run_id must be an integer",
        )

    form = await request.form()
    text = str(form.get("text", "")).strip()
    if not text:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="text is required",
        )

    with connect_db() as conn:
        row = conn.execute(
            "SELECT status FROM autofix_runs WHERE id = ?",
            (run_id_value,),
        ).fetchone()
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="run not found",
            )

        current_status = str(row["status"])
        if current_status not in RUN_HINT_EDITABLE_STATUSES:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="operator hints can only be appended to active runs",
            )

        try:
            append_run_operator_hint(conn, run_id_value, text)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(exc),
            ) from exc

    return JSONResponse(_load_run_detail(run_id_value))


@router.delete("/api/runs/{run_id}")
async def api_delete_run(run_id: str) -> JSONResponse:
    try:
        run_id_value = int(run_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="run_id must be an integer",
        )

    with connect_db() as conn:
        row = conn.execute(
            """
            SELECT id, repo, pr_number, status
            FROM autofix_runs
            WHERE id = ?
            """,
            (run_id_value,),
        ).fetchone()
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="run not found",
            )

        run_status = str(row["status"])
        if run_status in {"queued", "running", "cancel_requested", "retry_scheduled"}:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="active runs must be stopped before deletion",
            )

        repo = str(row["repo"])
        pr_number = int(row["pr_number"])

        conn.execute("DELETE FROM autofix_runs WHERE id = ?", (run_id_value,))
        remaining = conn.execute(
            """
            SELECT 1
            FROM autofix_runs
            WHERE repo = ? AND pr_number = ?
            LIMIT 1
            """,
            (repo, pr_number),
        ).fetchone()
        if remaining is None:
            conn.execute(
                "DELETE FROM pull_requests WHERE repo = ? AND pr_number = ?",
                (repo, pr_number),
            )
        conn.commit()

    return JSONResponse({"ok": True, "deleted_run_id": run_id_value})


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request) -> HTMLResponse:
    templates: Jinja2Templates = request.app.state.templates
    with connect_db() as conn:
        flag_context = build_feature_flag_context(conn)
        runtime_context = build_runtime_settings_context(conn)
        runtime_descriptions = [
            _serialize_runtime_setting_description(item)
            for item in describe_runtime_settings(conn)
            if not item.sensitive
        ]
    return templates.TemplateResponse(
        request=request,
        name="settings.html",
        context={
            "request": request,
            "title": "Software Factory - Settings",
            "saved": request.query_params.get("saved") == "1",
            "runtime_settings_descriptions": runtime_descriptions,
            **flag_context,
            **runtime_context,
        },
    )


@router.get("/api/settings/runtime")
async def runtime_settings_api() -> JSONResponse:
    with connect_db() as conn:
        descriptions = [
            _serialize_runtime_setting_description(item)
            for item in describe_runtime_settings(conn)
            if not item.sensitive
        ]
    return JSONResponse(
        {
            "settings": [item for item in descriptions if item["ownership"] == "db"],
            "env_only": [
                item for item in descriptions if item["ownership"] == "env_only"
            ],
        }
    )


@router.post("/settings", response_class=HTMLResponse)
async def save_settings(request: Request) -> RedirectResponse:
    form = await request.form()
    openhands_enabled = "agent_openhands_enabled" in form
    claude_agent_enabled = "agent_claude_agent_enabled" in form
    agent_primary_sdk = str(form.get("agent_primary_sdk", "claude_agent_sdk")).strip()

    openhands_command = str(form.get("openhands_command", "openhands")).strip()
    claude_agent_command = str(form.get("claude_agent_command", "claude")).strip()
    claude_agent_provider = str(form.get("claude_agent_provider", "zhipu")).strip()
    claude_agent_base_url = str(
        form.get("claude_agent_base_url", "https://open.bigmodel.cn/api/anthropic")
    ).strip()
    claude_agent_model = str(form.get("claude_agent_model", "glm-5")).strip()
    claude_agent_runtime = str(form.get("claude_agent_runtime", "host")).strip()
    claude_agent_container_image = str(
        form.get("claude_agent_container_image", "")
    ).strip()
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
        form.get("claude_agent_command_timeout_seconds", "1800")
    ).strip()
    try:
        claude_agent_command_timeout_seconds = max(1, int(claude_timeout_raw))
    except (TypeError, ValueError):
        claude_agent_command_timeout_seconds = 1800

    runtime_int_values = {
        field: _parse_form_int(form.get(field), default=default, minimum=minimum)
        for field, (default, minimum) in _RUNTIME_INT_FIELD_SPECS.items()
    }
    runtime_bot_logins = parse_settings_list_form_value(form.get("bot_logins_text"))
    runtime_noise_comment_patterns = parse_settings_list_form_value(
        form.get("noise_comment_patterns_text")
    )
    runtime_managed_repo_prefixes = parse_settings_list_form_value(
        form.get("managed_repo_prefixes_text")
    )
    runtime_autofix_comment_author = str(
        form.get("autofix_comment_author", "software-factory[bot]")
    ).strip()
    agent_sdks = build_selected_agent_sdks(
        agent_primary_sdk,
        openhands_enabled=openhands_enabled,
        claude_agent_enabled=claude_agent_enabled,
    )
    agent_flags = AgentFeatureFlags(
        agent_sdks=agent_sdks,
        openhands_command=openhands_command,
        openhands_command_timeout_seconds=openhands_command_timeout_seconds,
        openhands_worktree_base_dir=openhands_worktree_base_dir,
        claude_agent_command=claude_agent_command,
        claude_agent_provider=claude_agent_provider,
        claude_agent_base_url=claude_agent_base_url,
        claude_agent_model=claude_agent_model,
        claude_agent_runtime=claude_agent_runtime,
        claude_agent_container_image=claude_agent_container_image,
        claude_agent_command_timeout_seconds=claude_agent_command_timeout_seconds,
        claude_agent_worktree_base_dir=claude_agent_worktree_base_dir,
    )

    with connect_db() as conn:
        save_runtime_settings(
            conn,
            RuntimeSettingsPayload(
                github_webhook_debounce_seconds=runtime_int_values[
                    "github_webhook_debounce_seconds"
                ],
                max_autofix_per_pr=runtime_int_values["max_autofix_per_pr"],
                max_concurrent_runs=runtime_int_values["max_concurrent_runs"],
                stale_run_timeout_seconds=runtime_int_values[
                    "stale_run_timeout_seconds"
                ],
                pr_lock_ttl_seconds=runtime_int_values["pr_lock_ttl_seconds"],
                max_retry_attempts=runtime_int_values["max_retry_attempts"],
                retry_backoff_base_seconds=runtime_int_values[
                    "retry_backoff_base_seconds"
                ],
                retry_backoff_max_seconds=runtime_int_values[
                    "retry_backoff_max_seconds"
                ],
                bot_logins=runtime_bot_logins,
                noise_comment_patterns=runtime_noise_comment_patterns,
                managed_repo_prefixes=runtime_managed_repo_prefixes,
                autofix_comment_author=runtime_autofix_comment_author,
            ),
            changed_by="settings_ui",
            change_source="web.settings",
        )
        save_agent_feature_flags(
            conn,
            flags=agent_flags,
        )

    return RedirectResponse(url="/settings?saved=1", status_code=303)


def _parse_form_int(value: Any, *, default: int, minimum: int) -> int:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError, AttributeError):
        return default
    return parsed if parsed >= minimum else default


def _serialize_runtime_setting_description(value: Any) -> dict[str, Any]:
    return {
        "key": value.key,
        "label": value.label,
        "ownership": value.ownership,
        "sensitive": value.sensitive,
        "env_var": value.env_var,
        "effective": _serialize_runtime_setting_value(value.effective),
        "display_value": _display_runtime_setting_value(value.effective),
        "source": value.source,
        "updated_at": value.updated_at,
    }


def _serialize_runtime_setting_value(value: Any) -> Any:
    if isinstance(value, tuple):
        return list(value)
    return value


def _display_runtime_setting_value(value: Any) -> str:
    if isinstance(value, tuple):
        return ", ".join(str(item) for item in value) if value else "(empty)"
    text = str(value).strip()
    return text if text else "(empty)"


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
    request_data = {
        "url": str(form.get("url", "")).strip(),
        "description": str(form.get("description", "")),
        "project_root": str(form.get("project_root", "")),
        "dry_run": form.get("dry_run") == "true",
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
        target = _parse_task_submission(payload)
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

    description = _string_or_empty(payload.description) or None
    project_root = payload.project_root
    resolved_context = None
    if target.source_kind != TEXT_SOURCE_KIND:
        try:
            resolved_context = _resolve_manual_issue_context(
                target,
                description_present=description is not None,
            )
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
        result = _enqueue_task_fix(
            target=target,
            description=description,
            resolved_context=resolved_context,
            dry_run=payload.dry_run,
            project_root=project_root,
        )
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
            "message": "Validated" if payload.dry_run else "Submitted",
            "result": result,
            "form": request_data,
        },
    )


@router.post("/api/issues")
async def api_submit_issue(payload: IssueSubmissionRequest) -> dict[str, Any]:
    try:
        target = _parse_task_submission(payload)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    description = _string_or_empty(payload.description) or None
    project_root = payload.project_root
    resolved_context = None
    if target.source_kind != TEXT_SOURCE_KIND:
        try:
            resolved_context = _resolve_manual_issue_context(
                target,
                description_present=description is not None,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(exc),
            ) from exc

    try:
        return _enqueue_task_fix(
            target=target,
            description=description,
            resolved_context=resolved_context,
            dry_run=payload.dry_run,
            project_root=project_root,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except sqlite3.Error as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "ok": False,
                "message": "Failed to enqueue issue-based autofix",
                "error": str(exc),
            },
        ) from exc


@router.post("/api/issues/batch")
async def api_submit_issues_batch(request: Request) -> dict[str, Any]:
    content_type = request.headers.get("content-type", "")
    if not content_type.startswith("multipart/form-data"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Content-Type must be multipart/form-data",
        )

    form = await request.form()
    csv_file = form.get("file")
    if csv_file is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="CSV file is required. Upload a file with 'file' field name.",
        )

    try:
        from starlette.datastructures import UploadFile

        if isinstance(csv_file, UploadFile):
            content = await csv_file.read()
            text_content = (
                content.decode("utf-8") if isinstance(content, bytes) else content
            )
        elif isinstance(csv_file, str):
            text_content = csv_file
        else:
            content = csv_file.read()
            if hasattr(content, "decode"):
                text_content = content.decode("utf-8")
            else:
                text_content = str(content)
    except UnicodeDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="CSV file must be UTF-8 encoded",
        ) from exc

    reader = csv.DictReader(io.StringIO(text_content))
    if reader.fieldnames is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="CSV file must have a header row",
        )

    required_fields = {"url"}
    missing_fields = required_fields - set(reader.fieldnames)
    if missing_fields:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"CSV missing required columns: {', '.join(sorted(missing_fields))}",
        )

    results: list[dict[str, Any]] = []
    created_count = 0
    reused_count = 0
    validated_count = 0
    rejected_count = 0
    duplicate_count = 0

    for row_index, row in enumerate(reader):
        row_number = row_index + 2
        url = str(row.get("url", "")).strip()
        description = str(row.get("description", "")).strip() or None
        project_root = str(row.get("project_root", "")).strip() or None
        dry_run = _parse_bool_like(row.get("dry_run"))

        if not url:
            results.append(
                {
                    "row": row_number,
                    "url": url,
                    "status": "rejected",
                    "error": "URL is required",
                }
            )
            rejected_count += 1
            continue

        try:
            payload = IssueSubmissionRequest(
                url=url,
                description=description,
                project_root=project_root,
                dry_run=dry_run,
            )
        except ValidationError as exc:
            results.append(
                {
                    "row": row_number,
                    "url": url,
                    "status": "rejected",
                    "error": str(exc),
                }
            )
            rejected_count += 1
            continue

        try:
            target = _parse_task_submission(payload)
        except ValueError as exc:
            results.append(
                {
                    "row": row_number,
                    "url": url,
                    "status": "rejected",
                    "error": str(exc),
                }
            )
            rejected_count += 1
            continue

        try:
            resolved_context = _resolve_manual_issue_context(
                target,
                description_present=description is not None,
            )
        except ValueError as exc:
            results.append(
                {
                    "row": row_number,
                    "url": url,
                    "status": "rejected",
                    "error": str(exc),
                }
            )
            rejected_count += 1
            continue

        try:
            result = _enqueue_task_fix(
                target=target,
                description=description,
                resolved_context=resolved_context,
                dry_run=payload.dry_run,
                project_root=payload.project_root,
            )
        except ValueError as exc:
            results.append(
                {
                    "row": row_number,
                    "url": url,
                    "status": "rejected",
                    "error": str(exc),
                }
            )
            rejected_count += 1
            continue
        except sqlite3.Error as exc:
            results.append(
                {
                    "row": row_number,
                    "url": url,
                    "status": "rejected",
                    "error": f"Database error: {exc}",
                }
            )
            rejected_count += 1
            continue

        queue_status = result.get("queue_status", "")
        if queue_status == "validated":
            validated_count += 1
        elif queue_status == "reused_active_run":
            reused_count += 1
        elif queue_status == "queued":
            created_count += 1
        elif queue_status == "duplicate_task":
            duplicate_count += 1

        results.append(
            {
                "row": row_number,
                "url": url,
                "status": queue_status,
                "run_id": result.get("queued_run_id") or result.get("existing_run_id"),
                "repo": result.get("repo"),
                "pr_number": result.get("pr_number"),
            }
        )

    return {
        "ok": True,
        "message": "Batch processing completed",
        "summary": {
            "total": len(results),
            "created": created_count,
            "reused": reused_count,
            "validated": validated_count,
            "duplicates": duplicate_count,
            "rejected": rejected_count,
        },
        "results": results,
    }
