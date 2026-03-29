from __future__ import annotations

import hashlib
import sqlite3
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import ValidationError

from app.db import connect_db
from app.schemas.bug_input import (
    BugProviderKind,
    BugSubmissionRequest,
    BugSubmissionResponse,
)
from app.services.bug_input import (
    BUG_INPUT_PROVIDERS,
    build_bug_idempotency_key,
    resolve_provider,
)
from app.services.github_events import build_review_batch_id, build_task_idempotency_key
from app.services.policy import (
    ensure_pull_request_row,
    get_remaining_autofix_quota,
    reset_autofix_count_on_sha_change,
)
from app.services.queue import enqueue_autofix_run
from app.services.runtime_settings import resolve_runtime_settings


router = APIRouter(prefix="/api/bugs", tags=["bugs"])

_DEFAULT_REPO = "local/unspecified"
_SYNTHETIC_PR_OFFSET = 9_000_000


def _synthetic_pr_number(repo: str, title: str) -> int:
    raw = f"bug:{repo}:{title}"
    h = int(hashlib.sha256(raw.encode()).hexdigest()[:16], 16)
    return _SYNTHETIC_PR_OFFSET + (h % 999_999) + 1


def _enqueue_bug_fix(
    *,
    repo: str,
    title: str,
    source_url: str | None,
    normalized_review: dict[str, Any],
    trigger_source: str,
    dry_run: bool = False,
) -> dict[str, Any]:
    run_id: int | None = None
    remaining_quota: int | None = None
    idempotency_key: str | None = None
    queue_status = "not_queued"

    pr_number = int(normalized_review.get("pr_number", 0))
    head_sha = normalized_review.get("head_sha")

    review_batch_id = build_review_batch_id(normalized_review)
    normalized_review["review_batch_id"] = review_batch_id
    idempotency_key = build_bug_idempotency_key(
        repo=repo, title=title, source_url=source_url
    )

    if dry_run:
        return {
            "ok": True,
            "message": "Bug submission validated (dry run - no run created).",
            "repo": repo,
            "queue_status": "validated",
            "queued_run_id": None,
            "idempotency_key": idempotency_key,
            "remaining_quota": None,
            "head_sha": head_sha,
        }

    with connect_db() as conn:
        runtime_settings = resolve_runtime_settings(conn)
        if head_sha:
            reset_autofix_count_on_sha_change(conn, repo, pr_number, head_sha)
        ensure_pull_request_row(conn, repo, pr_number, branch=None, head_sha=head_sha)
        remaining_quota = get_remaining_autofix_quota(
            conn,
            repo,
            pr_number,
            max_autofix_per_pr=runtime_settings.max_autofix_per_pr,
        )
        if remaining_quota == 0:
            queue_status = "autofix_limit_reached"
        else:
            run_id = enqueue_autofix_run(
                conn=conn,
                repo=repo,
                pr_number=pr_number,
                head_sha=head_sha,
                normalized_review_json=normalized_review,
                trigger_source=trigger_source,
                idempotency_key=idempotency_key,
                max_attempts=runtime_settings.max_retry_attempts,
            )
            queue_status = "queued" if run_id is not None else "duplicate_task"

    return {
        "ok": True,
        "message": "Bug submission accepted.",
        "repo": repo,
        "queue_status": queue_status,
        "queued_run_id": run_id,
        "idempotency_key": idempotency_key,
        "remaining_quota": remaining_quota,
        "head_sha": head_sha,
    }


@router.post("", response_model=BugSubmissionResponse)
async def submit_bug(payload: BugSubmissionRequest) -> BugSubmissionResponse:
    bug_input = payload.to_bug_input()
    repo = bug_input.repo or _DEFAULT_REPO

    provider = resolve_provider(bug_input)
    synthetic_pr_number = _synthetic_pr_number(repo, bug_input.title)

    try:
        normalized_review = provider.to_normalized_review(
            bug_input,
            repo=repo,
            synthetic_pr_number=synthetic_pr_number,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to process bug input: {exc}",
        ) from exc

    try:
        result = _enqueue_bug_fix(
            repo=repo,
            title=bug_input.title,
            source_url=bug_input.source_url,
            normalized_review=normalized_review,
            trigger_source="bug_input",
            dry_run=payload.dry_run,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except sqlite3.Error as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Failed to enqueue bug fix: {exc}",
        ) from exc

    return BugSubmissionResponse(
        ok=result.get("ok", True),
        message=result.get("message", ""),
        repo=result.get("repo"),
        queue_status=result.get("queue_status", "not_queued"),
        queued_run_id=result.get("queued_run_id"),
        idempotency_key=result.get("idempotency_key"),
        remaining_quota=result.get("remaining_quota"),
        head_sha=result.get("head_sha"),
    )


@router.get("/providers")
async def list_providers() -> dict[str, Any]:
    providers = [{"kind": p.provider_kind} for p in BUG_INPUT_PROVIDERS]
    return {"ok": True, "providers": providers}
