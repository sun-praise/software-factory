from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Mapping, Literal


SUPPORTED_EVENT_TYPES = {
    "pull_request_review",
    "pull_request_review_comment",
    "issue_comment",
}


InsertStatus = Literal["inserted", "duplicate"]


@dataclass(frozen=True, slots=True)
class GitHubReviewEvent:
    repo: str
    pr_number: int
    event_type: str
    event_id: str | None
    event_key: str
    actor: str | None
    head_sha: str | None
    raw_payload_json: str


def extract_event_body(event_type: str, payload: Mapping[str, Any]) -> str | None:
    normalized_type = event_type.strip().lower()
    if normalized_type == "pull_request_review":
        review = payload.get("review")
        if isinstance(review, Mapping):
            return _as_str(review.get("body"))
        return None

    if normalized_type in {"pull_request_review_comment", "issue_comment"}:
        comment = payload.get("comment")
        if isinstance(comment, Mapping):
            return _as_str(comment.get("body"))
        return None

    return None


def build_review_batch_id(normalized_review: Mapping[str, Any]) -> str:
    digest = hashlib.sha256(
        json.dumps(normalized_review, ensure_ascii=True, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return digest[:16]


def build_task_idempotency_key(
    repo: str,
    pr_number: int,
    head_sha: str | None,
    review_batch_id: str,
    *,
    source_kind: str = "pull_request",
    issue_number: int | None = None,
) -> str:
    if source_kind == "issue":
        stable_issue_number = issue_number or 0
        return f"task:{repo}:issue:{stable_issue_number}:{review_batch_id}"
    stable_head_sha = (head_sha or "unknown").strip() or "unknown"
    return f"task:{repo}:{pr_number}:{stable_head_sha}:{review_batch_id}"


def extract_review_event(
    event_type: str, payload: Mapping[str, Any]
) -> GitHubReviewEvent | None:
    normalized_type = event_type.strip().lower()
    if normalized_type not in SUPPORTED_EVENT_TYPES:
        return None

    if normalized_type == "issue_comment" and not _is_pr_issue_comment(payload):
        return None

    repo = _extract_repo(payload)
    pr_number = _extract_pr_number(normalized_type, payload)
    if not repo or pr_number is None:
        return None

    event_id = _extract_event_id(normalized_type, payload)
    actor = _extract_actor(payload)
    head_sha = _extract_head_sha(payload)
    event_key = build_event_key(
        event_type=normalized_type,
        payload=payload,
        repo=repo,
        pr_number=pr_number,
        event_id=event_id,
        actor=actor,
        head_sha=head_sha,
    )

    return GitHubReviewEvent(
        repo=repo,
        pr_number=pr_number,
        event_type=normalized_type,
        event_id=event_id,
        event_key=event_key,
        actor=actor,
        head_sha=head_sha,
        raw_payload_json=json.dumps(payload, ensure_ascii=True, sort_keys=True),
    )


def build_event_key(
    event_type: str,
    payload: Mapping[str, Any],
    repo: str,
    pr_number: int,
    event_id: str | None,
    actor: str | None,
    head_sha: str | None,
) -> str:
    if event_id:
        return f"gh:{event_type}:{repo}:{pr_number}:{event_id}"

    stable_fields = {
        "event_type": event_type,
        "repo": repo,
        "pr_number": pr_number,
        "action": _as_str(payload.get("action")),
        "actor": actor,
        "head_sha": head_sha,
        "review": payload.get("review"),
        "comment": payload.get("comment"),
        "issue": payload.get("issue"),
    }
    digest = hashlib.sha256(
        json.dumps(stable_fields, ensure_ascii=True, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return f"gh:{event_type}:{repo}:{pr_number}:fallback:{digest}"


def insert_review_event(
    conn: sqlite3.Connection, event: GitHubReviewEvent
) -> InsertStatus:
    try:
        conn.execute(
            """
            INSERT INTO review_events (
                repo,
                pr_number,
                event_type,
                event_key,
                actor,
                head_sha,
                raw_payload_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.repo,
                event.pr_number,
                event.event_type,
                event.event_key,
                event.actor,
                event.head_sha,
                event.raw_payload_json,
            ),
        )
    except sqlite3.IntegrityError:
        return "duplicate"
    return "inserted"


def _extract_repo(payload: Mapping[str, Any]) -> str | None:
    repository = payload.get("repository")
    if isinstance(repository, Mapping):
        full_name = _as_str(repository.get("full_name"))
        if full_name:
            return full_name
    return _as_str(payload.get("repo"))


def _extract_pr_number(event_type: str, payload: Mapping[str, Any]) -> int | None:
    pull_request = payload.get("pull_request")
    if isinstance(pull_request, Mapping):
        from_pr = _as_int(pull_request.get("number"))
        if from_pr is not None:
            return from_pr

    if event_type == "issue_comment":
        issue = payload.get("issue")
        if isinstance(issue, Mapping):
            return _as_int(issue.get("number"))

    return _as_int(payload.get("number"))


def _extract_head_sha(payload: Mapping[str, Any]) -> str | None:
    pull_request = payload.get("pull_request")
    if isinstance(pull_request, Mapping):
        head = pull_request.get("head")
        if isinstance(head, Mapping):
            sha = _as_str(head.get("sha"))
            if sha:
                return sha

    review = payload.get("review")
    if isinstance(review, Mapping):
        commit_id = _as_str(review.get("commit_id"))
        if commit_id:
            return commit_id

    comment = payload.get("comment")
    if isinstance(comment, Mapping):
        commit_id = _as_str(comment.get("commit_id"))
        if commit_id:
            return commit_id

    return _as_str(payload.get("head_sha"))


def _extract_actor(payload: Mapping[str, Any]) -> str | None:
    sender = payload.get("sender")
    if isinstance(sender, Mapping):
        sender_login = _as_str(sender.get("login"))
        if sender_login:
            return sender_login

    for key in ("review", "comment"):
        nested = payload.get(key)
        if isinstance(nested, Mapping):
            user = nested.get("user")
            if isinstance(user, Mapping):
                login = _as_str(user.get("login"))
                if login:
                    return login

    return None


def _extract_event_id(event_type: str, payload: Mapping[str, Any]) -> str | None:
    object_key = {
        "pull_request_review": "review",
        "pull_request_review_comment": "comment",
        "issue_comment": "comment",
    }.get(event_type)

    if object_key:
        nested = payload.get(object_key)
        if isinstance(nested, Mapping):
            nested_id = _as_int(nested.get("id"))
            if nested_id is not None:
                return str(nested_id)

            node_id = _as_str(nested.get("node_id"))
            if node_id:
                return node_id

    direct_id = _as_int(payload.get("id"))
    if direct_id is not None:
        return str(direct_id)

    direct_node_id = _as_str(payload.get("node_id"))
    if direct_node_id:
        return direct_node_id

    return None


def _is_pr_issue_comment(payload: Mapping[str, Any]) -> bool:
    pull_request = payload.get("pull_request")
    if isinstance(pull_request, Mapping):
        return True

    issue = payload.get("issue")
    if not isinstance(issue, Mapping):
        return False

    issue_pr = issue.get("pull_request")
    return isinstance(issue_pr, Mapping)


def _as_str(value: Any) -> str | None:
    if isinstance(value, str):
        text = value.strip()
        if text:
            return text
    return None


def _as_int(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None
