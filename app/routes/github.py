import json
import sqlite3
from functools import lru_cache
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status

from app.config import get_settings
from app.db import connect_db
from app.services.debounce import InMemoryDebounceBackend
from app.services.github_events import extract_review_event, insert_review_event
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

    try:
        with connect_db() as conn:
            insert_status = insert_review_event(conn, event)
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
        "insert_status": insert_status,
        "debounce_window_seconds": debounce_backend.window_seconds,
        "debounce_ready": debounce_backend.is_ready(event.repo, event.pr_number),
    }
