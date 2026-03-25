import json
import sqlite3
from functools import lru_cache
from typing import Any
import re

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
    reset_autofix_count_on_sha_change,
)
from app.services.normalizer import normalize_review_events
from app.services.queue import enqueue_autofix_run
from app.services.github_signature import (
    GITHUB_SIGNATURE_HEADER,
    SignatureStatus,
    verify_github_signature,
)


router = APIRouter(prefix="/github", tags=["github"])
_REVIEW_EVENTS_ALLOWING_BOT_ACTORS = {
    "pull_request_review",
    "pull_request_review_comment",
}
_AUTOFIX_SUMMARY_COMMENT_PATTERN = re.compile(r"^\s*autofix run #\d+\b", re.IGNORECASE)


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


def _should_enqueue_for_event(event_type: str) -> bool:
    return event_type in {
        "pull_request_review",
        "pull_request_review_comment",
        "issue_comment",
    }


def _get_filter_reason_for_event(
    event_type: str,
    *,
    repo: str | None,
    actor: str | None,
    body: str | None,
) -> str | None:
    if _should_enqueue_for_event(event_type):
        return get_filter_reason(repo, actor=actor, body=body)
    return get_filter_reason(repo)


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

    if not event.head_sha and event.repo and event.pr_number:
        event, payload = _fill_pr_info_from_api(event, payload)

    should_ignore_actor = event_type in _REVIEW_EVENTS_ALLOWING_BOT_ACTORS
    event_body = extract_event_body(event_type, payload)
    if _is_autofix_summary_comment(event_type=event_type, body=event_body):
        return {
            "ok": True,
            "message": "GitHub webhook received",
            "event_type": event_type,
            "ignored": True,
            "reason": "autofix_summary_comment",
            "signature": signature_result.status,
            "repo": event.repo,
            "pr_number": event.pr_number,
        }
    filter_reason = _get_filter_reason_for_event(
        event_type,
        repo=event.repo,
        actor=None if should_ignore_actor else event.actor,
        body=event_body,
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
            # 先调用 reset 函数是为了在 SHA 变更时重置 autofix 计数
            # 对于新 PR（数据库无记录），reset 返回 False 是预期行为
            # head_sha 的更新由后续 ensure_pull_request_row 统一处理
            reset_autofix_count_on_sha_change(
                conn,
                event.repo,
                event.pr_number,
                event.head_sha,
            )
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
                if _should_enqueue_for_event(event_type):
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
                        queue_status = (
                            "queued" if run_id is not None else "duplicate_task"
                        )
                else:
                    queue_status = "recorded"
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
    ci_checks = _collect_ci_checks(events=events, head_sha=head_sha)
    normalized["branch"] = branch
    normalized["project_type"] = project_type or "python"
    if ci_checks:
        normalized["ci_checks"] = ci_checks
        normalized["ci_status"] = _summarize_ci_status(ci_checks)
    return normalized


def _collect_ci_checks(
    *, events: list[dict[str, Any]], head_sha: str | None
) -> list[dict[str, Any]]:
    latest_by_key: dict[str, dict[str, Any]] = {}
    for event in events:
        event_type = event.get("event_type")
        payload = event.get("payload")
        if not isinstance(event_type, str) or not isinstance(payload, dict):
            continue
        extracted = _extract_ci_check(event_type, payload)
        if extracted is None:
            continue
        event_head_sha = str(extracted.get("head_sha") or "").strip() or None
        if head_sha and event_head_sha and event_head_sha != head_sha:
            continue
        latest_by_key[extracted["key"]] = extracted

    checks = list(latest_by_key.values())
    checks.sort(
        key=lambda item: (
            _ci_sort_rank(
                str(item.get("conclusion") or ""), str(item.get("status") or "")
            ),
            str(item.get("name") or ""),
        )
    )
    return [
        {
            "source": item["source"],
            "name": item["name"],
            "status": item["status"],
            "conclusion": item["conclusion"],
            "details_url": item["details_url"],
            "head_sha": item["head_sha"],
        }
        for item in checks
    ]


def _extract_ci_check(
    event_type: str, payload: dict[str, Any]
) -> dict[str, Any] | None:
    if event_type not in {"check_run", "check_suite", "workflow_run"}:
        return None

    nested = payload.get(event_type)
    if not isinstance(nested, dict):
        return None

    check_id = nested.get("id")
    name = _as_text(nested.get("name"))
    if not name and event_type == "workflow_run":
        name = _as_text(nested.get("display_title"))
    if not name and event_type == "check_suite":
        app_info = nested.get("app")
        if isinstance(app_info, dict):
            name = _as_text(app_info.get("name"))
    if not name:
        name = event_type.replace("_", " ")

    return {
        "key": f"{event_type}:{check_id or name}",
        "source": event_type,
        "name": name,
        "status": _as_text(nested.get("status")) or "unknown",
        "conclusion": _as_text(nested.get("conclusion")) or "unknown",
        "details_url": _as_text(nested.get("details_url"))
        or _as_text(nested.get("html_url")),
        "head_sha": _as_text(nested.get("head_sha")),
    }


def _summarize_ci_status(ci_checks: list[dict[str, Any]]) -> str:
    if any(_is_failed_ci_check(item) for item in ci_checks):
        return "failed"
    if any(_is_pending_ci_check(item) for item in ci_checks):
        return "pending"
    if ci_checks:
        return "passed"
    return "unknown"


def _is_failed_ci_check(item: dict[str, Any]) -> bool:
    conclusion = _as_text(item.get("conclusion")) or ""
    return conclusion in {
        "failure",
        "cancelled",
        "timed_out",
        "action_required",
        "startup_failure",
        "stale",
    }


def _is_pending_ci_check(item: dict[str, Any]) -> bool:
    status_value = _as_text(item.get("status")) or ""
    if status_value and status_value != "completed":
        return True
    conclusion = _as_text(item.get("conclusion")) or ""
    return conclusion in {"queued", "in_progress", "pending", "waiting", "requested"}


def _ci_sort_rank(conclusion: str, status_value: str) -> int:
    if conclusion in {
        "failure",
        "cancelled",
        "timed_out",
        "action_required",
        "startup_failure",
        "stale",
    }:
        return 0
    if status_value and status_value != "completed":
        return 1
    return 2


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


def _is_autofix_summary_comment(*, event_type: str, body: str | None) -> bool:
    if event_type != "issue_comment":
        return False
    if not isinstance(body, str):
        return False
    return _AUTOFIX_SUMMARY_COMMENT_PATTERN.search(body) is not None


def _as_text(value: Any) -> str | None:
    if isinstance(value, str):
        text = value.strip()
        if text:
            return text
    return None


def _fill_pr_info_from_api(event, payload):
    """issue_comment 事件不含 head_sha/branch，通过 GitHub API 补获取。"""
    import dataclasses
    import logging
    import urllib.request

    log = logging.getLogger("webhook_debug")
    token = get_settings().github_token
    if not token:
        log.warning("GITHUB_TOKEN not set, cannot fetch PR info for %s#%s",
                     event.repo, event.pr_number)
        return event, payload

    url = f"https://api.github.com/repos/{event.repo}/pulls/{event.pr_number}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            pr_data = json.loads(resp.read().decode("utf-8"))
        head_sha = pr_data.get("head", {}).get("sha")
        if head_sha:
            log.info("Fetched head_sha=%s branch=%s for %s PR#%s",
                     head_sha, pr_data.get("head", {}).get("ref"),
                     event.repo, event.pr_number)
            event = dataclasses.replace(event, head_sha=head_sha)
            payload = dict(payload)
            payload["pull_request"] = pr_data
    except Exception as exc:
        log.warning("Failed to fetch PR info from GitHub API: %s", exc)
    return event, payload
