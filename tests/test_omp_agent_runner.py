from __future__ import annotations

from typing import Any
from unittest.mock import patch

from app.services.agent_modes import OMP_AGENT_MODE
from app.services import agent_runner
from app.services.feature_flags import AgentFeatureFlags


def _make_feature_flags(**overrides: Any) -> AgentFeatureFlags:
    """Create an AgentFeatureFlags with sensible defaults."""
    defaults: dict[str, Any] = {
        "enabled": True,
        "modes": ("omp", "claude_agent_sdk"),
        "claude_agent_command": "claude",
        "claude_agent_provider": "openrouter",
        "claude_agent_base_url": "https://openrouter.ai/api",
        "claude_agent_model": "openrouter/hunter-alpha",
        "claude_agent_runtime": "host",
        "claude_agent_container_image": "",
        "claude_agent_command_timeout_seconds": 600,
        "omp_command": "omp",
        "omp_provider": "anthropic",
        "omp_model": "claude-sonnet-4-20250514",
        "omp_thinking_level": "high",
        "omp_command_timeout_seconds": 1800,
        "ralph_command": "ralph",
        "ralph_command_timeout_seconds": 600,
        "openhands_command": "openhands",
        "openhands_command_timeout_seconds": 600,
    }
    defaults.update(overrides)
    return AgentFeatureFlags(**defaults)


def _base_execute_kwargs(**overrides: Any) -> dict[str, Any]:
    """Minimal kwargs for _execute_agent_sdks."""
    defaults: dict[str, Any] = {
        "workspace": "/tmp",
        "run_id": 123,
        "repo": "owner/repo",
        "pr_number": 1,
        "prompt": "fix this",
        "normalized_review": {},
        "modes": ("omp",),
        "ralph_command": "ralph",
        "ralph_command_timeout_seconds": 600,
        "openhands_command": "openhands",
        "openhands_command_timeout_seconds": 600,
        "claude_agent_command": "claude",
        "claude_agent_provider": "openrouter",
        "claude_agent_base_url": "https://openrouter.ai/api",
        "claude_agent_model": "openrouter/hunter-alpha",
        "claude_agent_runtime": "host",
        "claude_agent_container_image": "",
        "claude_agent_command_timeout_seconds": 600,
        "omp_command": "omp",
        "omp_provider": "anthropic",
        "omp_model": "claude-sonnet-4-20250514",
        "omp_thinking_level": "high",
        "omp_command_timeout_seconds": 1800,
    }
    defaults.update(overrides)
    return defaults


# ---------------------------------------------------------------------------
# Test 1: OMP_AGENT_MODE is importable and has expected value
# ---------------------------------------------------------------------------


def test_omp_agent_mode_in_modes() -> None:
    assert OMP_AGENT_MODE is not None
    assert OMP_AGENT_MODE == "omp"
    assert agent_runner.OMP_AGENT_MODE == "omp"
    assert agent_runner.OMP_FAILURE_CODE_COMMAND == "agent_omp_failed"


# ---------------------------------------------------------------------------
# Test 2: _execute_agent_sdks with OMP success
# ---------------------------------------------------------------------------


def test_execute_agent_sdks_omp_success(monkeypatch) -> None:
    calls: list[str] = []

    def fake_omp(
        workspace: str,
        run_id: int,
        repo: str,
        pr_number: int,
        prompt: str,
        normalized_review: dict[str, object],
        *,
        command: str,
        provider: str,
        model: str,
        thinking_level: str,
        timeout_seconds: int,
        repo_instructions: str | None = None,
        on_log_line: object | None = None,
        should_cancel: object | None = None,
        byok_overrides: object | None = None,
    ) -> tuple[bool, str, str | None]:
        calls.append("omp")
        return True, "completed", None

    monkeypatch.setattr(agent_runner, "_run_omp_agent_execution", fake_omp)

    ok, err_code, err_message, selected_mode = agent_runner._execute_agent_sdks(
        **_base_execute_kwargs(modes=("omp",)),
    )
    assert ok is True
    assert err_code is None
    assert err_message is None
    assert selected_mode == "omp"
    assert calls == ["omp"]


# ---------------------------------------------------------------------------
# Test 3: _execute_agent_sdks OMP failure falls through to claude
# ---------------------------------------------------------------------------


def test_execute_agent_sdks_omp_failure_fallback(monkeypatch) -> None:
    calls: list[str] = []

    def fake_omp(**kwargs) -> tuple[bool, str, str | None]:
        calls.append("omp")
        return False, "omp crashed", "agent_omp_failed"

    def fake_claude(**kwargs) -> tuple[bool, str, str | None]:
        calls.append("claude")
        return True, "claude succeeded", None

    monkeypatch.setattr(agent_runner, "_run_omp_agent_execution", fake_omp)
    monkeypatch.setattr(agent_runner, "_run_claude_agent", fake_claude)

    ok, err_code, err_message, selected_mode = agent_runner._execute_agent_sdks(
        **_base_execute_kwargs(modes=("omp", "claude_agent_sdk")),
    )
    assert ok is True
    assert err_code is None
    assert err_message is None
    assert selected_mode == "claude_agent_sdk"
    assert calls == ["omp", "claude"]


# ---------------------------------------------------------------------------
# Test 4: _execute_agent_sdks OMP cancelled
# ---------------------------------------------------------------------------


def test_execute_agent_sdks_omp_cancelled(monkeypatch) -> None:
    calls: list[str] = []

    def fake_omp(**kwargs) -> tuple[bool, str, str | None]:
        calls.append("omp")
        return False, "cancelled by user", agent_runner.RUN_CANCELLED_CODE

    monkeypatch.setattr(agent_runner, "_run_omp_agent_execution", fake_omp)

    ok, err_code, err_message, selected_mode = agent_runner._execute_agent_sdks(
        **_base_execute_kwargs(modes=("omp",)),
    )
    assert ok is False
    assert err_code == agent_runner.RUN_CANCELLED_CODE
    assert err_message == "cancelled by user"
    assert selected_mode == "omp"
    assert calls == ["omp"]


# ---------------------------------------------------------------------------
# Test 5: _run_omp_agent_execution command not found
# ---------------------------------------------------------------------------


def test_run_omp_agent_execution_command_not_found(tmp_path) -> None:
    with patch("app.services.agent_runner._command_exists", return_value=False):
        ok, message, error_code = agent_runner._run_omp_agent_execution(
            workspace=str(tmp_path),
            run_id=1,
            repo="owner/repo",
            pr_number=1,
            prompt="fix this",
            normalized_review={},
            command="nonexistent_omp_binary",
            provider="anthropic",
            model="claude-sonnet-4-20250514",
            thinking_level="high",
            timeout_seconds=300,
        )
    assert ok is False
    assert "not found" in message
    assert error_code == agent_runner.OMP_FAILURE_CODE_COMMAND


# ---------------------------------------------------------------------------
# Test 6: _run_omp_agent_execution empty command
# ---------------------------------------------------------------------------


def test_run_omp_agent_execution_empty_command(tmp_path) -> None:
    ok, message, error_code = agent_runner._run_omp_agent_execution(
        workspace=str(tmp_path),
        run_id=1,
        repo="owner/repo",
        pr_number=1,
        prompt="fix this",
        normalized_review={},
        command="",
        provider="anthropic",
        model="claude-sonnet-4-20250514",
        thinking_level="high",
        timeout_seconds=300,
    )
    assert ok is False
    assert "not configured" in message
    assert error_code == agent_runner.OMP_FAILURE_CODE_COMMAND
