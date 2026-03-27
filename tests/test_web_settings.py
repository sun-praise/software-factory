from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.config import get_settings
from app.db import init_db
from app.main import app
from app.services.feature_flags import resolve_agent_feature_flags
from app.services.runtime_settings import resolve_runtime_settings


def _setup_db(tmp_path: Path) -> Path:
    get_settings.cache_clear()
    db_path = tmp_path / "software_factory.db"
    import os

    os.environ["DB_PATH"] = str(db_path)
    init_db()
    return db_path


def test_settings_page_loads_defaults(tmp_path: Path) -> None:
    _setup_db(tmp_path)

    with TestClient(app) as client:
        response = client.get("/settings")

    assert response.status_code == 200
    html = response.text
    assert "System Settings" in html
    assert "Enable OpenHands agent mode" in html
    assert "Enable Claude Agent SDK mode" in html
    assert "GitHub webhook debounce window" in html
    assert "Max autofix runs per PR" in html
    assert "Managed repo prefixes" in html
    assert "Claude Agent provider" in html
    assert "Claude Agent runtime" in html
    assert "glm-5" in html
    assert "Zhipu Coding Plan" in html
    assert "software-factory/claude-agent:latest" in html


def test_save_settings_updates_feature_flags(tmp_path: Path) -> None:
    db_path = _setup_db(tmp_path)

    with TestClient(app) as client:
        response = client.post(
            "/settings",
            data={
                "agent_openhands_enabled": "on",
                "agent_claude_agent_enabled": "on",
                "github_webhook_debounce_seconds": "45",
                "max_autofix_per_pr": "7",
                "max_concurrent_runs": "5",
                "stale_run_timeout_seconds": "321",
                "pr_lock_ttl_seconds": "654",
                "max_retry_attempts": "4",
                "retry_backoff_base_seconds": "12",
                "retry_backoff_max_seconds": "900",
                "bot_logins_text": "ci-helper\ndependabot[bot]",
                "noise_comment_patterns_text": "^/retest\\b\n^/resolve\\b",
                "managed_repo_prefixes_text": "acme/\nwidgets/",
                "autofix_comment_author": "autofix-bot",
                "openhands_command": "openhands-test",
                "openhands_command_timeout_seconds": "123",
                "openhands_worktree_base_dir": "tmp/worktrees",
                "claude_agent_command": "claude-test",
                "claude_agent_provider": "deepseek",
                "claude_agent_base_url": "https://api.deepseek.com/anthropic",
                "claude_agent_model": "deepseek-chat",
                "claude_agent_runtime": "docker",
                "claude_agent_container_image": "ghcr.io/example/claude-code:latest",
                "claude_agent_command_timeout_seconds": "222",
                "claude_agent_worktree_base_dir": "tmp/claude-worktrees",
            },
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"] == "/settings?saved=1"

    import sqlite3

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        flags = {
            row["key"]: row["value"]
            for row in conn.execute(
                "SELECT key, value FROM app_feature_flags"
            ).fetchall()
        }

    assert flags["agent.openhands.enabled"] == "1"
    assert flags["agent.claude_agent.enabled"] == "1"
    assert flags["runtime.github_webhook_debounce_seconds"] == "45"
    assert flags["runtime.max_autofix_per_pr"] == "7"
    assert flags["runtime.max_concurrent_runs"] == "5"
    assert flags["runtime.stale_run_timeout_seconds"] == "321"
    assert flags["runtime.pr_lock_ttl_seconds"] == "654"
    assert flags["runtime.max_retry_attempts"] == "4"
    assert flags["runtime.retry_backoff_base_seconds"] == "12"
    assert flags["runtime.retry_backoff_max_seconds"] == "900"
    assert flags["runtime.bot_logins"] == '["ci-helper", "dependabot[bot]"]'
    assert (
        flags["runtime.noise_comment_patterns"] == '["^/retest\\\\b", "^/resolve\\\\b"]'
    )
    assert flags["runtime.managed_repo_prefixes"] == '["acme/", "widgets/"]'
    assert flags["runtime.autofix_comment_author"] == "autofix-bot"
    assert flags["agent.claude_agent.command"] == "claude-test"
    assert flags["agent.claude_agent.provider"] == "deepseek"
    assert flags["agent.claude_agent.base_url"] == "https://api.deepseek.com/anthropic"
    assert flags["agent.claude_agent.model"] == "deepseek-chat"
    assert flags["agent.claude_agent.runtime"] == "docker"
    assert (
        flags["agent.claude_agent.container_image"]
        == "ghcr.io/example/claude-code:latest"
    )
    assert flags["agent.claude_agent.command_timeout_seconds"] == "222"
    assert flags["agent.claude_agent.worktree_base_dir"] == "tmp/claude-worktrees"
    assert flags["agent.openhands.command"] == "openhands-test"
    assert flags["agent.openhands.command_timeout_seconds"] == "123"
    assert flags["agent.openhands.worktree_base_dir"] == "tmp/worktrees"
    assert flags["agent.legacy.enabled"] == "1"

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        runtime_settings = resolve_runtime_settings(conn)
        active_flags = resolve_agent_feature_flags(conn)

    assert runtime_settings.github_webhook_debounce_seconds == 45
    assert runtime_settings.max_autofix_per_pr == 7
    assert runtime_settings.max_concurrent_runs == 5
    assert runtime_settings.stale_run_timeout_seconds == 321
    assert runtime_settings.pr_lock_ttl_seconds == 654
    assert runtime_settings.max_retry_attempts == 4
    assert runtime_settings.retry_backoff_base_seconds == 12
    assert runtime_settings.retry_backoff_max_seconds == 900
    assert runtime_settings.bot_logins == ("ci-helper", "dependabot[bot]")
    assert runtime_settings.noise_comment_patterns == (r"^/retest\b", r"^/resolve\b")
    assert runtime_settings.managed_repo_prefixes == ("acme/", "widgets/")
    assert runtime_settings.autofix_comment_author == "autofix-bot"
    assert active_flags.openhands_command == "openhands-test"
    assert active_flags.openhands_command_timeout_seconds == 123
    assert active_flags.openhands_worktree_base_dir == "tmp/worktrees"
    assert active_flags.claude_agent_command == "claude-test"
    assert active_flags.claude_agent_provider == "deepseek"
    assert active_flags.claude_agent_base_url == "https://api.deepseek.com/anthropic"
    assert active_flags.claude_agent_model == "deepseek-chat"
    assert active_flags.claude_agent_runtime == "docker"
    assert (
        active_flags.claude_agent_container_image
        == "ghcr.io/example/claude-code:latest"
    )
    assert active_flags.claude_agent_command_timeout_seconds == 222
    assert active_flags.claude_agent_worktree_base_dir == "tmp/claude-worktrees"
    assert "openhands" in active_flags.agent_sdks
    assert "claude_agent_sdk" in active_flags.agent_sdks
