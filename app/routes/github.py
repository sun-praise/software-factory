import json
import sqlite3
from functools import lru_cache
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status

from app.config import get_settings
from app.db import connect_db
from app.services.debounce import InMemoryDebounceBackend
from app.services.filter import get_filter_reason
from app.services.github_events import (
    build_review_batch_id,
    build_task_idempotency_key,
    extract_event_body,
    extract_review_event,
    insert_review_event,
)
from app.services.policy import (
    ensure_pull_request_row,
    get_remaining_autofix_quota,
    is_autofix_limit_reached,
)
from app.services.normalizer import normalize_review_events
from app.services.queue import enqueue_autofix_run
from app.services.github_signature import (
    GITHUB_SIGNATURE_HEADER,
    SignatureStatus,
    verify_github_signature,
)


router = APIRouter(prefix="/github", tags=["github"])


async def _read_payload(request: Request) -> dict[str, Any]:
    body = await request.body()
    if not body:
        return {}
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {"raw": data}


@lru_cache
def _get_debounce_backend() -> InMemoryDebounceBackend:
    window_seconds = get_settings().github_webhook_debounce_seconds
    return InMemoryDebounceBackend(window_seconds=window_seconds)


@router.post("/webhook")
async def github_webhook(request: Request) -> dict[str, Any]:
    raw_body = await request.body()
    signature_result = verify_github_signature(
        body=raw_body,
        secret=get_settings().github_webhook_secret,
        signature_header=request.headers.get(GITHUB_SIGNATURE_HEADER),
    )
    if signature_result.status == SignatureStatus.FAILED:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "ok": False,
                "message": "Invalid GitHub webhook signature",
                "reason": signature_result.reason,
            },
        )

    payload = await _read_payload(request)
    event_type = request.headers.get("x-github-event", "unknown").strip().lower()
    event = extract_review_event(event_type=event_type, payload=payload)
    if event is None:
        return {
            "ok": True,
            "message": "GitHub webhook received",
            "event_type": event_type,
            "ignored": True,
            "reason": "unsupported_or_non_pr_event",
            "signature": signature_result.status,
        }

    filter_reason = get_filter_reason(
        event.repo,
        actor=event.actor,
        body=extract_event_body(event_type, payload),
    )
    if filter_reason is not None:
        return {
            "ok": True,
            "message": "GitHub webhook received",
            "event_type": event_type,
            "ignored": True,
            "reason": filter_reason,
            "signature": signature_result.status,
            "repo": event.repo,
            "pr_number": event.pr_number,
        }

    run_id: int | None = None
    idempotency_key: str | None = None
    queue_status = "not_queued"
    remaining_quota: int | None = None
    try:
        with connect_db() as conn:
            ensure_pull_request_row(
                conn,
                event.repo,
                event.pr_number,
                branch=_extract_branch_from_payload(payload),
                head_sha=event.head_sha,
            )
            insert_status = insert_review_event(conn, event)
            if insert_status == "inserted":
                normalized_review = _build_normalized_review(
                    conn=conn,
                    repo=event.repo,
                    pr_number=event.pr_number,
                    head_sha=event.head_sha,
                )
                review_batch_id = build_review_batch_id(normalized_review)
                normalized_review["review_batch_id"] = review_batch_id
                idempotency_key = build_task_idempotency_key(
                    repo=event.repo,
                    pr_number=event.pr_number,
                    head_sha=event.head_sha,
                    review_batch_id=review_batch_id,
                )
                remaining_quota = get_remaining_autofix_quota(
                    conn,
                    event.repo,
                    event.pr_number,
                )
                if remaining_quota == 0:
                    queue_status = "autofix_limit_reached"
                else:
                    run_id = enqueue_autofix_run(
                        conn=conn,
                        repo=event.repo,
                        pr_number=event.pr_number,
                        head_sha=event.head_sha,
                        normalized_review_json=normalized_review,
                        trigger_source="github_webhook",
                        idempotency_key=idempotency_key,
                        max_attempts=get_settings().max_retry_attempts,
                    )
                    queue_status = "queued" if run_id is not None else "duplicate_task"
            else:
                queue_status = "duplicate_event"
    except sqlite3.Error as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "ok": False,
                "message": "Failed to persist webhook event",
                "error": str(exc),
            },
        ) from exc

    debounce_backend = _get_debounce_backend()
    debounce_backend.record_event(repo=event.repo, pr_number=event.pr_number)

    return {
        "ok": True,
        "message": "GitHub webhook received",
        "event_type": event_type,
        "signature": signature_result.status,
        "repo": event.repo,
        "pr_number": event.pr_number,
        "event_key": event.event_key,
        "idempotency_key": idempotency_key,
        "insert_status": insert_status,
        "queue_status": queue_status,
        "queued_run_id": run_id,
        "remaining_quota": remaining_quota,
        "debounce_window_seconds": debounce_backend.window_seconds,
        "debounce_ready": debounce_backend.is_ready(event.repo, event.pr_number),
    }


def _build_normalized_review(
    conn: sqlite3.Connection,
    repo: str,
    pr_number: int,
    head_sha: str | None,
) -> dict[str, Any]:
    rows = conn.execute(
        """
        SELECT event_type, raw_payload_json
        FROM review_events
        WHERE repo = ? AND pr_number = ?
        ORDER BY id ASC
        """,
        (repo, pr_number),
    ).fetchall()

    events: list[dict[str, Any]] = []
    branch: str | None = None
    project_type: str | None = None
    for row in rows:
        event_type = row["event_type"]
        payload = _parse_row_payload(row["raw_payload_json"])
        if payload is None:
            continue
        if branch is None:
            branch = _extract_branch_from_payload(payload)
        if project_type is None:
            project_type = _extract_project_type_from_payload(payload)
        events.append({"event_type": event_type, "payload": payload})

    normalized = normalize_review_events(
        repo=repo,
        pr_number=pr_number,
        events=events,
        head_sha=head_sha,
    )
    normalized["branch"] = branch
    normalized["project_type"] = project_type or "python"
    return normalized


def _parse_row_payload(raw_payload_json: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(raw_payload_json)
    except json.JSONDecodeError:
        return None
    if isinstance(payload, dict):
        return payload
    return None


def _extract_branch_from_payload(payload: dict[str, Any]) -> str | None:
    pull_request = payload.get("pull_request")
    if not isinstance(pull_request, dict):
        return None
    head = pull_request.get("head")
    if not isinstance(head, dict):
        return None
    ref = head.get("ref")
    if isinstance(ref, str) and ref.strip():
        return ref.strip()
    return None


def _extract_project_type_from_payload(payload: dict[str, Any]) -> str | None:
    repository = payload.get("repository")
    if not isinstance(repository, dict):
        return None
    language = repository.get("language")
    if not isinstance(language, str):
        return None

    normalized = language.strip().lower()
    mapping = {
        "python": "python",
        "javascript": "node",
        "typescript": "node",
        "go": "go",
        "rust": "rust",
    }
    return mapping.get(normalized)
