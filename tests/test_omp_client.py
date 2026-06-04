"""Tests for app.services.omp_client."""

from __future__ import annotations

import subprocess
import time
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.services.omp_client import (
    _build_omp_argv,
    run_omp_agent,
    session_has_prior_context,
)


# ---------------------------------------------------------------------------
# session_has_prior_context
# ---------------------------------------------------------------------------


def test_session_has_prior_context_empty(tmp_path: Path) -> None:
    """Empty directory returns False."""
    assert session_has_prior_context(str(tmp_path)) is False


def test_session_has_prior_context_with_jsonl(tmp_path: Path) -> None:
    """Directory containing a .jsonl file returns True."""
    (tmp_path / "session.jsonl").write_text("{}\n")
    assert session_has_prior_context(str(tmp_path)) is True


def test_session_has_prior_context_none() -> None:
    """None input returns False."""
    assert session_has_prior_context(None) is False


# ---------------------------------------------------------------------------
# _build_omp_argv
# ---------------------------------------------------------------------------


def test_build_omp_argv_minimal() -> None:
    """Minimal arguments produce the expected argv."""
    argv = _build_omp_argv(
        omp_command="omp",
        model="sonnet",
        provider="",
        thinking_level="off",
        session_dir=None,
        append_system_prompt=None,
        prompt="do the thing",
    )
    assert argv[0] == "omp"
    assert "-p" in argv
    assert "--model" in argv
    idx = argv.index("--model")
    assert argv[idx + 1] == "sonnet"
    # Provider omitted when empty
    assert "--provider" not in argv
    # Thinking omitted when "off"
    assert "--thinking" not in argv
    # Prompt is the last element
    assert argv[-1] == "do the thing"


def test_build_omp_argv_full() -> None:
    """All arguments are forwarded correctly."""
    argv = _build_omp_argv(
        omp_command="/usr/local/bin/omp",
        model="opus",
        provider="anthropic",
        thinking_level="high",
        session_dir="/tmp/sess",
        append_system_prompt="be helpful",
        prompt="hello",
    )
    assert argv[0] == "/usr/local/bin/omp"
    assert "--model" in argv
    assert "opus" in argv
    assert "--provider" in argv
    assert "anthropic" in argv
    assert "--thinking" in argv
    assert "high" in argv
    assert "--session-dir" in argv
    assert "/tmp/sess" in argv
    assert "--append-system-prompt" in argv
    assert "be helpful" in argv
    assert argv[-1] == "hello"


def test_build_omp_argv_continue_when_session_exists(tmp_path: Path) -> None:
    """When session_dir contains .jsonl, --continue is appended."""
    (tmp_path / "turn1.jsonl").write_text("{}\n")
    argv = _build_omp_argv(
        omp_command="omp",
        model="sonnet",
        provider="",
        thinking_level="off",
        session_dir=str(tmp_path),
        append_system_prompt=None,
        prompt="go",
    )
    assert "--continue" in argv


def test_build_omp_argv_no_continue_when_session_empty(tmp_path: Path) -> None:
    """When session_dir has no .jsonl, --continue is absent."""
    argv = _build_omp_argv(
        omp_command="omp",
        model="sonnet",
        provider="",
        thinking_level="off",
        session_dir=str(tmp_path),
        append_system_prompt=None,
        prompt="go",
    )
    assert "--continue" not in argv


# ---------------------------------------------------------------------------
# Mock Popen helper
# ---------------------------------------------------------------------------


class _FakePopen:
    """Minimal mock of subprocess.Popen used by run_omp_agent."""

    def __init__(
        self,
        stdout_text: str = "",
        stderr_text: str = "",
        returncode: int = 0,
        poll_iterations: int = 2,
        *,
        hang_forever: bool = False,
    ) -> None:
        self.stdout = StringIO(stdout_text)
        self.stderr = StringIO(stderr_text)
        self.returncode: int | None = None
        self.pid = 42
        self._final_rc = returncode
        self._poll_count = 0
        self._poll_iterations = poll_iterations
        self._hang_forever = hang_forever
        self._killed = False

    def poll(self) -> int | None:
        if self._hang_forever and not self._killed:
            return None
        self._poll_count += 1
        if self._poll_count >= self._poll_iterations:
            self.returncode = self._final_rc
            return self._final_rc
        return None

    def communicate(self, timeout: float | None = None) -> tuple[str, str]:
        return (self.stdout.getvalue(), self.stderr.getvalue())

    def kill(self) -> None:
        self._killed = True
        self.returncode = -9

    def terminate(self) -> None:
        self._killed = True
        self.returncode = -15

    def __enter__(self) -> _FakePopen:
        return self

    def __exit__(self, *args: object) -> None:
        pass


def _patch_popen(monkeypatch: pytest.MonkeyPatch, fake: _FakePopen) -> None:
    """Replace subprocess.Popen with a factory that returns *fake*."""
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *a, **kw: fake,
    )


def _patch_terminate(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch _terminate_process_tree to avoid real signals."""

    def _fake_terminate(proc: subprocess.Popen[str]) -> None:
        proc.returncode = -9

    monkeypatch.setattr(
        "app.services.omp_client._terminate_process_tree",
        _fake_terminate,
    )


def _patch_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make time.sleep a no-op inside omp_client."""
    monkeypatch.setattr("app.services.omp_client.time.sleep", lambda _: None)


# ---------------------------------------------------------------------------
# run_omp_agent
# ---------------------------------------------------------------------------


def test_run_omp_agent_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """Successful run returns (True, message, None)."""
    fake = _FakePopen(stdout_text="all done\n", returncode=0, poll_iterations=1)
    _patch_popen(monkeypatch, fake)
    _patch_sleep(monkeypatch)

    ok, msg, err = run_omp_agent(
        workspace="/tmp/ws",
        prompt="do it",
        model="sonnet",
        provider="anthropic",
        thinking_level="high",
    )
    assert ok is True
    assert "all done" in msg
    assert err is None


def test_run_omp_agent_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-zero exit returns (False, message, 'agent_omp_failed')."""
    fake = _FakePopen(
        stdout_text="",
        stderr_text="something broke",
        returncode=1,
        poll_iterations=1,
    )
    _patch_popen(monkeypatch, fake)
    _patch_sleep(monkeypatch)

    ok, msg, err = run_omp_agent(
        workspace="/tmp/ws",
        prompt="do it",
        model="sonnet",
        provider="",
        thinking_level="off",
    )
    assert ok is False
    assert "something broke" in msg
    assert err == "agent_omp_failed"


def test_run_omp_agent_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Timeout returns (False, message, 'agent_omp_failed')."""
    fake = _FakePopen(hang_forever=True)
    _patch_popen(monkeypatch, fake)
    _patch_terminate(monkeypatch)
    _patch_sleep(monkeypatch)

    # monotonic: first call (deadline calc) returns 0, rest return 9999.
    _call_count = 0

    def _fake_monotonic() -> float:
        nonlocal _call_count
        _call_count += 1
        if _call_count <= 1:
            return 0.0  # deadline = 0 + timeout_seconds
        return 9999.0  # past any reasonable deadline

    monkeypatch.setattr("app.services.omp_client.time.monotonic", _fake_monotonic)

    ok, msg, err = run_omp_agent(
        workspace="/tmp/ws",
        prompt="do it",
        model="sonnet",
        provider="",
        thinking_level="off",
        timeout_seconds=1,
    )
    assert ok is False
    assert "timed out" in msg
    assert err == "agent_omp_failed"


def test_run_omp_agent_cancel(monkeypatch: pytest.MonkeyPatch) -> None:
    """should_cancel returning True aborts with 'cancelled' error code."""
    fake = _FakePopen(hang_forever=True)
    _patch_popen(monkeypatch, fake)
    _patch_terminate(monkeypatch)
    _patch_sleep(monkeypatch)

    ok, msg, err = run_omp_agent(
        workspace="/tmp/ws",
        prompt="do it",
        model="sonnet",
        provider="",
        thinking_level="off",
        should_cancel=lambda: True,
    )
    assert ok is False
    assert "cancelled" in msg.lower()
    assert err == "cancelled"


def test_run_omp_agent_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    """env_overrides are layered into the subprocess environment."""
    captured_env: dict[str, str] = {}

    def _capture_popen(argv, **kwargs):
        captured_env.update(kwargs.get("env", {}))
        return _FakePopen(stdout_text="ok", returncode=0, poll_iterations=1)

    monkeypatch.setattr(subprocess, "Popen", _capture_popen)
    _patch_sleep(monkeypatch)

    run_omp_agent(
        workspace="/tmp/ws",
        prompt="do it",
        model="sonnet",
        provider="",
        thinking_level="off",
        env_overrides={"MY_CUSTOM_VAR": "42"},
    )
    assert captured_env.get("MY_CUSTOM_VAR") == "42"


def test_run_omp_agent_on_log_line(monkeypatch: pytest.MonkeyPatch) -> None:
    """on_log_line callback receives prefixed output lines."""
    fake = _FakePopen(
        stdout_text="line one\nline two\n",
        stderr_text="err line\n",
        returncode=0,
        poll_iterations=1,
    )
    _patch_popen(monkeypatch, fake)
    _patch_sleep(monkeypatch)

    log_lines: list[str] = []
    run_omp_agent(
        workspace="/tmp/ws",
        prompt="do it",
        model="sonnet",
        provider="",
        thinking_level="off",
        on_log_line=log_lines.append,
    )

    # Should contain at least one [omp] stdout line and the starting line.
    omp_out = [l for l in log_lines if l.startswith("[omp] ")]
    omp_err = [l for l in log_lines if l.startswith("[omp:err] ")]
    assert len(omp_out) >= 1
    assert len(omp_err) >= 1
    # The starting line should mention the command.
    assert any("starting:" in l for l in omp_out)
