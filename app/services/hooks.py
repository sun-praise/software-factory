from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
from typing import Any

from app.db import connect_db

logger = logging.getLogger(__name__)

UNKNOWN_REPO = "unknown/unknown"
UNKNOWN_BRANCH = "unknown"


def process_hook_event(
    payload: dict[str, Any], header_event_type: str
) -> dict[str, Any]:
    event_name = _resolve_event_name(payload, header_event_type)
    base_result: dict[str, Any] = {
        "action": "ignored",
        "linked_pr_number": None,
        "event_key": None,
        "session_row_id": None,
    }

    try:
        with connect_db() as conn:
            if event_name == "UserPromptSubmit":
                session_id = _register_session(conn, payload)
                linked_pr_number = _link_pr_for_session(conn, session_id, payload)
                base_result.update(
                    {
                        "action": "session_registered",
                        "linked_pr_number": linked_pr_number,
                        "session_row_id": session_id,
                    }
                )
                return base_result

            if event_name in {"PostToolUse", "PostToolUseFailure"}:
                result = _record_tool_event(conn, event_name, payload)
                base_result.update(
                    {
                        "action": "tool_event_recorded",
                        "linked_pr_number": result["linked_pr_number"],
                        "event_key": result["event_key"],
                        "session_row_id": result["session_row_id"],
                    }
                )
                return base_result
    except sqlite3.Error as exc:
        logger.exception("Database error processing hook event: %s", exc)
        base_result.update(
            {
                "action": "error",
                "error": f"DatabaseError: {exc}",
            }
        )
        return base_result
    except Exception as exc:
        logger.exception("Unexpected error processing hook event: %s", exc)
        base_result.update(
            {
                "action": "error",
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
        return base_result

    return base_result


def _register_session(conn: Any, payload: dict[str, Any]) -> int:
    repo = _extract_repo(payload)
    branch = _extract_branch(payload)
    cwd = _extract_cwd(payload)
    session_key = _extract_session_key(payload)

    existing = None
    if session_key:
        existing = conn.execute(
            """
            SELECT id, repo, branch, cwd, metadata_json
            FROM sessions
            WHERE json_extract(metadata_json, '$.session_id') = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (session_key,),
        ).fetchone()

    if existing is None and repo and branch:
        existing = conn.execute(
            """
            SELECT id, repo, branch, cwd, metadata_json
            FROM sessions
            WHERE repo = ? AND branch = ? AND ended_at IS NULL
            ORDER BY id DESC
            LIMIT 1
            """,
            (repo, branch),
        ).fetchone()

    metadata = _read_metadata(existing["metadata_json"] if existing else None)
    metadata["last_hook_event"] = "UserPromptSubmit"
    if session_key:
        metadata["session_id"] = session_key
    if cwd:
        metadata["cwd"] = cwd

    metadata_json = json.dumps(metadata, ensure_ascii=True, sort_keys=True)

    if existing:
        resolved_repo = repo or str(existing["repo"])
        resolved_branch = branch or str(existing["branch"])
        resolved_cwd = cwd or _extract_text(existing["cwd"])
        conn.execute(
            """
            UPDATE sessions
            SET repo = ?, branch = ?, cwd = ?, metadata_json = ?
            WHERE id = ?
            """,
            (
                resolved_repo,
                resolved_branch,
                resolved_cwd,
                metadata_json,
                existing["id"],
            ),
        )
        return int(existing["id"])

    cursor = conn.execute(
        """
        INSERT INTO sessions (repo, branch, cwd, metadata_json)
        VALUES (?, ?, ?, ?)
        """,
        (repo or UNKNOWN_REPO, branch or UNKNOWN_BRANCH, cwd, metadata_json),
    )
    return int(cursor.lastrowid)


def _record_tool_event(
    conn: Any, event_name: str, payload: dict[str, Any]
) -> dict[str, Any]:
    session_row = _find_session(conn, payload)

    repo = _extract_repo(payload) or (session_row["repo"] if session_row else None)
    branch = _extract_branch(payload) or (
        session_row["branch"] if session_row else None
    )
    pr_number = _extract_pr_number(payload)
    head_sha = _extract_head_sha(payload)

    linked_pr_number = _link_pull_request(
        conn=conn,
        repo=repo,
        branch=branch,
        pr_number=pr_number,
        head_sha=head_sha,
        linked_session_id=int(session_row["id"]) if session_row else None,
    )

    if session_row:
        metadata = _read_metadata(session_row["metadata_json"])
        metadata["last_hook_event"] = event_name
        if linked_pr_number is not None:
            metadata["linked_pr_number"] = linked_pr_number
        conn.execute(
            "UPDATE sessions SET metadata_json = ? WHERE id = ?",
            (
                json.dumps(metadata, ensure_ascii=True, sort_keys=True),
                int(session_row["id"]),
            ),
        )

    event_key = _extract_event_key(event_name, payload)

    conn.execute(
        """
        INSERT OR IGNORE INTO review_events (repo, pr_number, event_type, event_key, actor, head_sha, raw_payload_json)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            repo or UNKNOWN_REPO,
            linked_pr_number if linked_pr_number is not None else 0,
            event_name,
            event_key,
            _extract_actor(payload),
            head_sha,
            json.dumps(payload, ensure_ascii=True, sort_keys=True),
        ),
    )

    return {
        "linked_pr_number": linked_pr_number,
        "event_key": event_key,
        "session_row_id": int(session_row["id"]) if session_row else None,
    }


def _link_pr_for_session(
    conn: Any, session_id: int, payload: dict[str, Any]
) -> int | None:
    session_row = conn.execute(
        "SELECT id, repo, branch, metadata_json FROM sessions WHERE id = ?",
        (session_id,),
    ).fetchone()
    if session_row is None:
        return None

    linked_pr_number = _link_pull_request(
        conn=conn,
        repo=session_row["repo"],
        branch=session_row["branch"],
        pr_number=_extract_pr_number(payload),
        head_sha=_extract_head_sha(payload),
        linked_session_id=session_id,
    )

    metadata = _read_metadata(session_row["metadata_json"])
    if linked_pr_number is not None:
        metadata["linked_pr_number"] = linked_pr_number
    conn.execute(
        "UPDATE sessions SET metadata_json = ? WHERE id = ?",
        (json.dumps(metadata, ensure_ascii=True, sort_keys=True), session_id),
    )
    return linked_pr_number


def _link_pull_request(
    conn: Any,
    repo: str | None,
    branch: str | None,
    pr_number: int | None,
    head_sha: str | None,
    linked_session_id: int | None,
) -> int | None:
    if not repo:
        return None

    if pr_number is not None:
        conn.execute(
            """
            INSERT INTO pull_requests (repo, pr_number, head_sha, branch, linked_session_id, updated_at)
            VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(repo, pr_number) DO UPDATE SET
                head_sha = COALESCE(excluded.head_sha, pull_requests.head_sha),
                branch = COALESCE(excluded.branch, pull_requests.branch),
                linked_session_id = COALESCE(excluded.linked_session_id, pull_requests.linked_session_id),
                updated_at = CURRENT_TIMESTAMP
            """,
            (repo, pr_number, head_sha, branch, linked_session_id),
        )
        return pr_number

    if branch:
        row = conn.execute(
            """
            SELECT pr_number
            FROM pull_requests
            WHERE repo = ? AND branch = ?
            ORDER BY updated_at DESC, id DESC
            LIMIT 1
            """,
            (repo, branch),
        ).fetchone()
        if row:
            if linked_session_id is not None:
                conn.execute(
                    """
                    UPDATE pull_requests
                    SET linked_session_id = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE repo = ? AND pr_number = ?
                    """,
                    (linked_session_id, repo, int(row["pr_number"])),
                )
            return int(row["pr_number"])

    return None


def _find_session(conn: Any, payload: dict[str, Any]) -> Any:
    session_key = _extract_session_key(payload)
    if session_key:
        row = conn.execute(
            """
            SELECT id, repo, branch, metadata_json
            FROM sessions
            WHERE json_extract(metadata_json, '$.session_id') = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (session_key,),
        ).fetchone()
        if row:
            return row

    repo = _extract_repo(payload)
    branch = _extract_branch(payload)
    if repo and branch:
        return conn.execute(
            """
            SELECT id, repo, branch, metadata_json
            FROM sessions
            WHERE repo = ? AND branch = ? AND ended_at IS NULL
            ORDER BY id DESC
            LIMIT 1
            """,
            (repo, branch),
        ).fetchone()

    return None


def _extract_repo(payload: dict[str, Any]) -> str | None:
    for source in _candidate_maps(payload):
        value = source.get("repo")
        if isinstance(value, str) and value.strip():
            return value.strip()

        repository = source.get("repository")
        if isinstance(repository, dict):
            full_name = repository.get("full_name")
            if isinstance(full_name, str) and full_name.strip():
                return full_name.strip()

    return None


SESSION_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{8,128}$")


def _extract_session_key(payload: dict[str, Any]) -> str | None:
    for source in _candidate_maps(payload):
        session_id = source.get("session_id")
        if isinstance(session_id, str) and session_id.strip():
            clean_id = session_id.strip()
            if SESSION_ID_PATTERN.match(clean_id):
                return clean_id
    return None


def _extract_cwd(payload: dict[str, Any]) -> str | None:
    for source in _candidate_maps(payload):
        cwd = source.get("cwd")
        if isinstance(cwd, str) and cwd.strip():
            return cwd.strip()
    return None


def _extract_branch(payload: dict[str, Any]) -> str | None:
    for source in _candidate_maps(payload):
        branch = source.get("branch")
        if isinstance(branch, str) and branch.strip():
            value = branch.strip()
            return value.removeprefix("refs/heads/")
    return None


def _extract_pr_number(payload: dict[str, Any]) -> int | None:
    for source in _candidate_maps(payload):
        direct = source.get("pr_number")
        if isinstance(direct, int):
            return direct
        if isinstance(direct, str) and direct.isdigit():
            return int(direct)

        pull_request = source.get("pull_request")
        if isinstance(pull_request, dict):
            number = pull_request.get("number")
            if isinstance(number, int):
                return number
            if isinstance(number, str) and number.isdigit():
                return int(number)

    return None


def _extract_head_sha(payload: dict[str, Any]) -> str | None:
    for source in _candidate_maps(payload):
        for key in ("head_sha", "headSha", "commit_sha", "commitSha"):
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

        pull_request = source.get("pull_request")
        if isinstance(pull_request, dict):
            head = pull_request.get("head")
            if isinstance(head, dict):
                sha = head.get("sha")
                if isinstance(sha, str) and sha.strip():
                    return sha.strip()

    return None


def _extract_actor(payload: dict[str, Any]) -> str | None:
    for source in _candidate_maps(payload):
        for key in ("tool_name", "tool", "actor"):
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _extract_event_key(event_name: str, payload: dict[str, Any]) -> str:
    for source in _candidate_maps(payload):
        explicit_key = source.get("event_key")
        if isinstance(explicit_key, str) and explicit_key.strip():
            return explicit_key.strip()

    stable_payload = json.dumps(payload, ensure_ascii=True, sort_keys=True)
    digest = hashlib.sha1(f"{event_name}:{stable_payload}".encode("utf-8")).hexdigest()
    return f"hook:{event_name}:{digest}"


def _extract_text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _read_metadata(raw_value: str | None) -> dict[str, Any]:
    if not raw_value:
        return {}
    try:
        loaded = json.loads(raw_value)
    except json.JSONDecodeError:
        return {}
    if isinstance(loaded, dict):
        return loaded
    return {}


def _resolve_event_name(payload: dict[str, Any], header_event_type: str) -> str:
    for source in _candidate_maps(payload):
        for key in ("event", "event_type", "hook_event_name"):
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

    if isinstance(header_event_type, str) and header_event_type.strip():
        return header_event_type.strip()

    return "unknown"


def _candidate_maps(payload: dict[str, Any]) -> list[dict[str, Any]]:
    maps: list[dict[str, Any]] = [payload]

    nested_payload = payload.get("payload")
    if isinstance(nested_payload, dict):
        maps.append(nested_payload)

    metadata = payload.get("metadata")
    if isinstance(metadata, dict):
        maps.append(metadata)

    return maps
