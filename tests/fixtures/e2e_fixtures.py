from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app
from app.models import SCHEMA_SQL
from app.routes.github import _get_debounce_backend
from app.services.ai_client import FixPlan
from app.services.agent_runner import RunnerOps
from app.services.github_signature import build_signature
from app.db import init_db
from app.services.patch_applier import ApplyResult


def make_in_memory_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    return conn


def setup_e2e_env(tmp_path: Path, secret: str = "test-secret") -> Path:
    get_settings.cache_clear()
    _get_debounce_backend.cache_clear()
    db_path = tmp_path / "software_factory.db"
    import os

    os.environ["DB_PATH"] = str(db_path)
    os.environ["GITHUB_WEBHOOK_SECRET"] = secret
    os.environ["GITHUB_WEBHOOK_DEBOUNCE_SECONDS"] = "60"
    os.environ["OPENHANDS_COMMAND"] = "true"
    os.environ["MAX_AUTOFIX_PER_PR"] = "3"
    os.environ["MAX_RETRY_ATTEMPTS"] = "3"
    os.environ["BOT_LOGINS"] = "github-actions[bot],dependabot[bot]"
    os.environ["NOISE_COMMENT_PATTERNS"] = r"^/retest\b,^/resolve\b"
    os.environ["MANAGED_REPO_PREFIXES"] = "acme/"
    os.environ["MAX_CONCURRENT_RUNS"] = "5"
    init_db()
    return db_path


def make_pull_request_review_payload(
    repo: str = "acme/widgets",
    pr_number: int = 42,
    head_sha: str = "abc123def456",
    review_id: int = 1001,
    actor: str = "reviewer",
    body: str = "Please fix this",
) -> dict[str, Any]:
    return {
        "repository": {"full_name": repo, "language": "Python"},
        "pull_request": {
            "number": pr_number,
            "head": {"sha": head_sha, "ref": "feature/test"},
        },
        "review": {"id": review_id, "body": body},
        "sender": {"login": actor},
    }


def make_issue_comment_payload(
    repo: str = "acme/widgets",
    issue_number: int = 42,
    comment_id: int = 3001,
    actor: str = "commenter",
    body: str = "please re-run",
    is_pr: bool = True,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "repository": {"full_name": repo},
        "issue": {"number": issue_number},
        "comment": {"id": comment_id, "body": body},
        "sender": {"login": actor},
    }
    if is_pr:
        payload["issue"]["pull_request"] = {
            "url": f"https://github.com/{repo}/pull/{issue_number}"
        }
    return payload


def sign_payload(payload: dict[str, Any], secret: str) -> str:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return "sha256=" + build_signature(body=body, secret=secret)


def post_webhook(
    client: TestClient,
    event_type: str,
    payload: dict[str, Any],
    secret: str = "",
) -> Any:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    headers = {
        "content-type": "application/json",
        "X-GitHub-Event": event_type,
    }
    if secret:
        headers["X-Hub-Signature-256"] = "sha256=" + build_signature(
            body=body, secret=secret
        )
    return client.post("/github/webhook", content=body, headers=headers)


def make_mock_runner_ops(
    checkout_ok: bool = True,
    ensure_head_ok: bool = True,
    commit_success: bool = True,
    comment_ok: bool = True,
    commit_sha: str = "deadbeef1234",
    commit_error: str | None = None,
) -> RunnerOps:
    return RunnerOps(
        checkout_branch=lambda *_: (
            checkout_ok,
            "checked out" if checkout_ok else "failed",
        ),
        ensure_head_sha=lambda *_: ensure_head_ok,
        commit_and_push=lambda **_: {
            "success": commit_success,
            "commit_sha": commit_sha if commit_success else None,
            "error": commit_error if not commit_success else None,
        },
        post_pr_comment=lambda *_: (comment_ok, "ok" if comment_ok else "api error"),
        generate_fix=lambda **_: FixPlan(summary="updated file", changes=()),
        apply_fix_plan=lambda **_: ApplyResult(changed_files=("app/main.py",)),
    )


def make_mock_executor(results: list[dict[str, Any]] | None = None) -> MagicMock:
    if results is None:
        results = [{"returncode": 0, "stdout": "ok", "stderr": ""}]
    call_count = [0]

    def executor(command: str, workspace_dir: str) -> dict[str, Any]:
        idx = min(call_count[0], len(results) - 1)
        call_count[0] += 1
        return results[idx]

    return MagicMock(side_effect=executor)


def get_db_connection(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def count_runs(db_path: Path, status: str | None = None) -> int:
    with get_db_connection(db_path) as conn:
        if status:
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM autofix_runs WHERE status = ?",
                (status,),
            ).fetchone()
        else:
            row = conn.execute("SELECT COUNT(*) as cnt FROM autofix_runs").fetchone()
        return row["cnt"] if row else 0


def get_run_by_id(db_path: Path, run_id: int) -> dict[str, Any] | None:
    with get_db_connection(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM autofix_runs WHERE id = ?", (run_id,)
        ).fetchone()
        if row is None:
            return None
        return {key: row[key] for key in row.keys()}


def get_pr_autofix_count(db_path: Path, repo: str, pr_number: int) -> int:
    with get_db_connection(db_path) as conn:
        row = conn.execute(
            "SELECT autofix_count FROM pull_requests WHERE repo = ? AND pr_number = ?",
            (repo, pr_number),
        ).fetchone()
        return row["autofix_count"] if row else 0


def set_pr_autofix_count(db_path: Path, repo: str, pr_number: int, count: int) -> None:
    with get_db_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO pull_requests (repo, pr_number, autofix_count)
            VALUES (?, ?, ?)
            ON CONFLICT(repo, pr_number) DO UPDATE SET autofix_count = ?
            """,
            (repo, pr_number, count, count),
        )
        conn.commit()


def count_review_events(db_path: Path, repo: str, pr_number: int) -> int:
    with get_db_connection(db_path) as conn:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM review_events WHERE repo = ? AND pr_number = ?",
            (repo, pr_number),
        ).fetchone()
        return row["cnt"] if row else 0
