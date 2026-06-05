from __future__ import annotations

import sqlite3

from app.services.runtime_settings import resolve_runtime_settings


def ensure_pull_request_row(
    conn: sqlite3.Connection,
    repo: str,
    pr_number: int,
    *,
    branch: str | None = None,
    head_sha: str | None = None,
) -> sqlite3.Row:
    conn.execute(
        """
        INSERT INTO pull_requests (repo, pr_number, branch, head_sha, updated_at)
        VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(repo, pr_number) DO UPDATE SET
            branch = COALESCE(excluded.branch, pull_requests.branch),
            head_sha = COALESCE(excluded.head_sha, pull_requests.head_sha),
            updated_at = CURRENT_TIMESTAMP
        """,
        (repo, pr_number, branch, head_sha),
    )
    row = conn.execute(
        "SELECT * FROM pull_requests WHERE repo = ? AND pr_number = ? LIMIT 1",
        (repo, pr_number),
    ).fetchone()
    if row is None:
        raise RuntimeError("failed to initialize pull request row")
    return row


def get_autofix_count(conn: sqlite3.Connection, repo: str, pr_number: int) -> int:
    row = ensure_pull_request_row(conn, repo, pr_number)
    return int(row["autofix_count"])


def get_remaining_autofix_quota(
    conn: sqlite3.Connection,
    repo: str,
    pr_number: int,
    *,
    max_autofix_per_pr: int | None = None,
) -> int:
    limit = _resolve_max_autofix_per_pr(conn, max_autofix_per_pr)
    count = get_autofix_count(conn, repo, pr_number)
    remaining = limit - count
    return remaining if remaining > 0 else 0


def is_autofix_limit_reached(
    conn: sqlite3.Connection,
    repo: str,
    pr_number: int,
    *,
    max_autofix_per_pr: int | None = None,
) -> bool:
    return (
        get_remaining_autofix_quota(
            conn,
            repo,
            pr_number,
            max_autofix_per_pr=max_autofix_per_pr,
        )
        == 0
    )


def increment_autofix_count(
    conn: sqlite3.Connection,
    repo: str,
    pr_number: int,
    *,
    amount: int = 1,
    branch: str | None = None,
    head_sha: str | None = None,
) -> int:
    if amount < 1:
        raise ValueError("amount must be positive")

    ensure_pull_request_row(
        conn,
        repo,
        pr_number,
        branch=branch,
        head_sha=head_sha,
    )
    conn.execute(
        """
        UPDATE pull_requests
        SET autofix_count = autofix_count + ?, updated_at = CURRENT_TIMESTAMP
        WHERE repo = ? AND pr_number = ?
        """,
        (amount, repo, pr_number),
    )
    return get_autofix_count(conn, repo, pr_number)


def _resolve_max_autofix_per_pr(
    conn: sqlite3.Connection,
    value: int | None,
) -> int:
    limit = (
        value
        if value is not None
        else resolve_runtime_settings(conn).max_autofix_per_pr
    )
    if limit < 0:
        raise ValueError("max_autofix_per_pr must be non-negative")
    return limit


def _safe_text(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def reset_autofix_count_on_sha_change(
    conn: sqlite3.Connection,
    repo: str,
    pr_number: int,
    new_head_sha: str | None,
) -> bool:
    """Reset autofix_count when head_sha changes to a never-before-seen SHA.

    Tracks seen SHAs in the `seen_shas` column to prevent ping-pong attacks
    (SHA-A → SHA-B → SHA-A) from bypassing the autofix limit.

    Note: This function does NOT commit. Caller is responsible for transaction management.

    Returns:
        True if reset was performed, False otherwise.
    """
    if not new_head_sha:
        return False
    row = conn.execute(
        "SELECT head_sha, seen_shas FROM pull_requests WHERE repo = ? AND pr_number = ? LIMIT 1",
        (repo, pr_number),
    ).fetchone()
    if row is None:
        return False
    old_sha = _safe_text(row["head_sha"])
    if not old_sha or old_sha == new_head_sha:
        return False
    seen_raw = row["seen_shas"] or ""
    if new_head_sha in seen_raw.split(","):
        return False
    # Add both the old (now-seen) and new SHA to the tracking set
    parts = [p for p in seen_raw.split(",") if p]
    if old_sha not in parts:
        parts.append(old_sha)
    parts.append(new_head_sha)
    new_seen = ",".join(parts)
    conn.execute(
        "UPDATE pull_requests SET autofix_count = 0, seen_shas = ?, updated_at = CURRENT_TIMESTAMP WHERE repo = ? AND pr_number = ?",
        (new_seen, repo, pr_number),
    )
    return True
