from __future__ import annotations

import re
from typing import Any, Mapping


_IGNORE_TEXTS = {
    "+1",
    "lgtm",
    "thanks",
    "thank you",
    "thx",
    "ty",
}


def normalize_review_events(
    repo: str,
    pr_number: int,
    events: list[dict],
    head_sha: str | None = None,
) -> dict:
    must_fix: list[dict[str, Any]] = []
    should_fix: list[dict[str, Any]] = []
    ignore: list[dict[str, Any]] = []
    seen: set[tuple[str, str | None, int | None, str]] = set()

    for event in events:
        if not isinstance(event, Mapping):
            continue

        event_type_raw = event.get("event_type")
        payload_raw = event.get("payload")
        if not isinstance(event_type_raw, str) or not isinstance(payload_raw, Mapping):
            continue

        event_type = event_type_raw.strip().lower()
        payload: Mapping[str, Any] = payload_raw

        candidate = _extract_candidate(event_type, payload)
        if candidate is None:
            continue

        dedupe_key = (
            candidate["source"],
            candidate["path"],
            candidate["line"],
            _normalize_text_for_dedupe(candidate["text"]),
        )
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)

        item = {
            "source": candidate["source"],
            "path": candidate["path"],
            "line": candidate["line"],
            "text": candidate["text"],
            "severity": classify_severity(candidate["text"]),
        }

        if _is_ignorable_text(candidate["text"]):
            ignore.append(item)
        elif candidate["is_must_fix"]:
            must_fix.append(item)
        else:
            should_fix.append(item)

    summary = (
        f"{len(must_fix)} blocking issues, "
        f"{len(should_fix)} suggestions, "
        f"{len(ignore)} ignored"
    )

    return {
        "repo": repo,
        "pr_number": pr_number,
        "head_sha": head_sha,
        "must_fix": must_fix,
        "should_fix": should_fix,
        "ignore": ignore,
        "summary": summary,
    }


def classify_severity(text: str) -> str:
    normalized = text.lower()
    if _contains_any(normalized, ("security", "critical", "crash", "data loss")):
        return "P0"
    if _contains_any(normalized, ("null", "none", "exception", "error", "fail", "bug")):
        return "P1"
    if _contains_any(normalized, ("refactor", "maintainability", "performance")):
        return "P2"
    return "P3"


def _extract_candidate(
    event_type: str, payload: Mapping[str, Any]
) -> dict[str, Any] | None:
    if event_type == "pull_request_review":
        review = payload.get("review")
        if not isinstance(review, Mapping):
            return None

        body = _as_text(review.get("body"))
        state = _as_text(review.get("state"))
        return {
            "source": "pull_request_review",
            "path": None,
            "line": None,
            "text": body,
            "is_must_fix": (state or "").lower() == "changes_requested",
        }

    if event_type == "pull_request_review_comment":
        comment = payload.get("comment")
        if not isinstance(comment, Mapping):
            return None

        body = _as_text(comment.get("body"))
        path = _as_text(comment.get("path"))
        line = _as_int(comment.get("line"))
        if line is None:
            line = _as_int(comment.get("start_line"))
        return {
            "source": "pull_request_review_comment",
            "path": path,
            "line": line,
            "text": body,
            "is_must_fix": True,
        }

    if event_type == "issue_comment":
        issue = payload.get("issue")
        if not isinstance(issue, Mapping):
            return None
        if not isinstance(issue.get("pull_request"), Mapping):
            return None

        comment = payload.get("comment")
        if not isinstance(comment, Mapping):
            return None

        body = _as_text(comment.get("body"))
        return {
            "source": "issue_comment",
            "path": None,
            "line": None,
            "text": body,
            "is_must_fix": False,
        }

    return None


def _normalize_text_for_dedupe(text: str) -> str:
    lowered = text.lower().strip()
    return re.sub(r"\s+", " ", lowered)


def _contains_any(text: str, terms: tuple[str, ...]) -> bool:
    return any(term in text for term in terms)


def _as_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    return ""


def _as_int(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _is_ignorable_text(text: str) -> bool:
    normalized = _normalize_text_for_dedupe(text)
    return not normalized or normalized in _IGNORE_TEXTS
