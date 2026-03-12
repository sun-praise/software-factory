from __future__ import annotations

import hashlib
import json
from typing import Any

from app.db import connect_db


def process_hook_event(
    payload: dict[str, Any], header_event_type: str
) -> dict[str, Any]:
    event_name = str(payload.get("event") or header_event_type or "unknown")

    with connect_db() as conn:
        if event_name == "UserPromptSubmit":
            session_id = _register_session(conn, payload)
            linked_pr_number = _link_pr_for_session(conn, session_id, payload)
            return {
                "action": "session_registered",
                "linked_pr_number": linked_pr_number,
                "session_row_id": session_id,
            }

        if event_name in {"PostToolUse", "PostToolUseFailure"}:
            result = _record_tool_event(conn, event_name, payload)
            return {
                "action": "tool_event_recorded",
                "linked_pr_number": result["linked_pr_number"],
                "event_key": result["event_key"],
                "session_row_id": result["session_row_id"],
            }

    return {
        "action": "ignored",
        "linked_pr_number": None,
    }


def _register_session(conn: Any, payload: dict[str, Any]) -> int:
    repo = _extract_repo(payload)
    branch = _extract_branch(payload)
    cwd = _extract_text(payload.get("cwd"))
    session_key = _extract_text(payload.get("session_id"))

    existing = None
    if session_key:
        existing = conn.execute(
            """
            SELECT id, metadata_json
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
            SELECT id, metadata_json
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
        conn.execute(
            """
            UPDATE sessions
            SET repo = ?, branch = ?, cwd = ?, metadata_json = ?
            WHERE id = ?
            """,
            (
                repo or "unknown/unknown",
                branch or "unknown",
                cwd,
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
        (repo or "unknown/unknown", branch or "unknown", cwd, metadata_json),
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
            repo or "unknown/unknown",
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
    session_key = _extract_text(payload.get("session_id"))
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
    value = payload.get("repo")
    if isinstance(value, str) and value.strip():
        return value.strip()

    repository = payload.get("repository")
    if isinstance(repository, dict):
        full_name = repository.get("full_name")
        if isinstance(full_name, str) and full_name.strip():
            return full_name.strip()

    return None


def _extract_branch(payload: dict[str, Any]) -> str | None:
    branch = payload.get("branch")
    if isinstance(branch, str) and branch.strip():
        value = branch.strip()
        return value.removeprefix("refs/heads/")
    return None


def _extract_pr_number(payload: dict[str, Any]) -> int | None:
    direct = payload.get("pr_number")
    if isinstance(direct, int):
        return direct
    if isinstance(direct, str) and direct.isdigit():
        return int(direct)

    pull_request = payload.get("pull_request")
    if isinstance(pull_request, dict):
        number = pull_request.get("number")
        if isinstance(number, int):
            return number
        if isinstance(number, str) and number.isdigit():
            return int(number)

    return None


def _extract_head_sha(payload: dict[str, Any]) -> str | None:
    for key in ("head_sha", "headSha", "commit_sha", "commitSha"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    pull_request = payload.get("pull_request")
    if isinstance(pull_request, dict):
        head = pull_request.get("head")
        if isinstance(head, dict):
            sha = head.get("sha")
            if isinstance(sha, str) and sha.strip():
                return sha.strip()

    return None


def _extract_actor(payload: dict[str, Any]) -> str | None:
    for key in ("tool_name", "tool", "actor"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _extract_event_key(event_name: str, payload: dict[str, Any]) -> str:
    explicit_key = payload.get("event_key")
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
