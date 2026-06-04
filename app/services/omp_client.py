"""Drive the ``omp`` (oh-my-pi CLI) agent as a subprocess.

This module is the omp counterpart of the Claude Agent SDK subprocess path
in :mod:`app.services.agent_runner`.  It spawns ``omp -p`` (non-interactive
mode), streams stdout/stderr back through callbacks, and enforces a
wall-clock timeout with clean process-group teardown.

Public API
----------
run_omp_agent          – spawn an omp session and wait for it to finish.
session_has_prior_context – check whether a session directory already holds a
                            JSONL transcript (for ``--continue`` decisions).
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path

logger = logging.getLogger(__name__)

_TERMINATE_GRACE_PERIOD_SECONDS = 5.0


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def session_has_prior_context(session_dir: str | None) -> bool:
    """Return *True* if *session_dir* already contains an omp JSONL transcript."""
    if not session_dir:
        return False
    try:
        return any(Path(session_dir).glob("*.jsonl"))
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Core runner
# ---------------------------------------------------------------------------

def run_omp_agent(
    *,
    workspace: str,
    prompt: str,
    model: str,
    provider: str,
    thinking_level: str,
    session_dir: str | None = None,
    append_system_prompt: str | None = None,
    timeout_seconds: int = 1800,
    omp_command: str = "omp",
    on_log_line: Callable[[str], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
    env_overrides: Mapping[str, str] | None = None,
) -> tuple[bool, str, str | None]:
    """Run the ``omp`` CLI agent in non-interactive mode.

    Parameters
    ----------
    workspace:
        Working directory for the subprocess.
    prompt:
        The task prompt passed as the final positional argument.
    model:
        Model identifier forwarded via ``--model``.
    provider:
        Provider name forwarded via ``--provider``.  Omitted when empty.
    thinking_level:
        Thinking budget forwarded via ``--thinking``.  Ignored when ``"off"``.
    session_dir:
        If given, forwarded via ``--session-dir``.  When the directory already
        contains ``*.jsonl`` files, ``--continue`` is appended automatically.
    append_system_prompt:
        Extra system-prompt text appended via ``--append-system-prompt``.
    timeout_seconds:
        Wall-clock limit for the entire subprocess run.
    omp_command:
        Base command to invoke (default ``"omp"``).
    on_log_line:
        Optional callback invoked for each line of output (prefixed).
    should_cancel:
        Optional predicate polled once per second; returning *True* aborts
        the run.
    env_overrides:
        Extra environment variables layered on top of ``os.environ``.

    Returns
    -------
    tuple[bool, str, str | None]
        ``(success, message, error_code)`` – same contract as
        ``_run_claude_agent``.
    """

    argv = _build_omp_argv(
        omp_command=omp_command,
        model=model,
        provider=provider,
        thinking_level=thinking_level,
        session_dir=session_dir,
        append_system_prompt=append_system_prompt,
        prompt=prompt,
    )

    env = dict(os.environ)
    if env_overrides:
        env.update(env_overrides)

    display_argv = list(argv)

    # Substitute the prompt with a short placeholder for logging to avoid
    # flooding logs with potentially large prompts.
    if argv and argv[-1] == prompt:
        display_argv = list(argv[:-1]) + [f"<prompt ({len(prompt)} chars)>"]

    failure_code = "agent_omp_failed"

    # ---- spawn ----
    process: subprocess.Popen[str]
    try:
        process = subprocess.Popen(
            argv,
            cwd=workspace,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
            env=env,
            start_new_session=True,
        )
    except FileNotFoundError:
        return False, f"omp command not found: {argv[0]}", failure_code
    except OSError as exc:
        return False, f"omp command failed to start: {exc}", failure_code

    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []

    if on_log_line is not None:
        on_log_line(
            "[omp] starting: "
            + " ".join(_shell_quote(t) for t in display_argv)
        )

    # ---- stream readers ----
    stdout_stream = getattr(process, "stdout", None)
    stderr_stream = getattr(process, "stderr", None)

    stdout_thread = threading.Thread(
        target=_consume_stream,
        args=(stdout_stream, "stdout", stdout_chunks, on_log_line),
        daemon=True,
    )
    stderr_thread = threading.Thread(
        target=_consume_stream,
        args=(stderr_stream, "stderr", stderr_chunks, on_log_line),
        daemon=True,
    )
    stdout_thread.start()
    stderr_thread.start()

    # ---- poll loop ----
    try:
        deadline = time.monotonic() + timeout_seconds
        while True:
            # Cancellation check.
            if should_cancel is not None and should_cancel():
                if on_log_line is not None:
                    on_log_line("[omp] cancellation requested; terminating process")
                _terminate_process_tree(process)
                return False, "omp command cancelled by user", "cancelled"

            # Process exited?
            if process.poll() is not None:
                break

            # Timeout check.
            if time.monotonic() >= deadline:
                _terminate_process_tree(process)
                return (
                    False,
                    f"omp command timed out after {timeout_seconds}s",
                    failure_code,
                )

            time.sleep(1.0)
    except OSError as exc:
        _terminate_process_tree(process)
        return False, f"omp command failed while running: {exc}", failure_code
    finally:
        stdout_thread.join(timeout=3.0)
        stderr_thread.join(timeout=3.0)

    # ---- collect results ----
    stdout = "".join(stdout_chunks).strip()
    stderr = "".join(stderr_chunks).strip()

    if process.returncode != 0:
        message = _build_failure_message(
            returncode=process.returncode,
            stdout=stdout,
            stderr=stderr,
        )
        return False, message, failure_code

    return True, stdout or "omp completed", None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _build_omp_argv(
    *,
    omp_command: str,
    model: str,
    provider: str,
    thinking_level: str,
    session_dir: str | None,
    append_system_prompt: str | None,
    prompt: str,
) -> list[str]:
    """Construct the ``omp`` command-line argument list."""
    argv: list[str] = [omp_command, "-p"]

    # Non-interactive / automation flags
    argv.append("--auto-approve")
    argv.append("--no-title")
    argv.append("--no-skills")
    argv.append("--no-rules")

    # Model selection
    argv.extend(["--model", model])

    # Provider (optional)
    if provider:
        argv.extend(["--provider", provider])

    # Thinking level (skip when "off")
    if thinking_level and thinking_level != "off":
        argv.extend(["--thinking", thinking_level])

    # Session directory
    if session_dir:
        argv.extend(["--session-dir", session_dir])
        if session_has_prior_context(session_dir):
            argv.append("--continue")

    # Extra system prompt
    if append_system_prompt:
        argv.extend(["--append-system-prompt", append_system_prompt])

    # Positional prompt (last)
    argv.append(prompt)

    return argv


def _consume_stream(
    stream: object | None,
    stream_name: str,
    chunks: list[str],
    on_log_line: Callable[[str], None] | None,
) -> None:
    """Read *stream* line-by-line, appending raw text to *chunks*.

    Each rendered line is forwarded to *on_log_line* (if provided) with a
    ``[omp]`` / ``[omp:err]`` prefix.  This is the omp equivalent of
    ``_consume_process_stream`` in ``agent_runner.py``.
    """
    if stream is None:
        return
    prefix = "[omp]" if stream_name == "stdout" else "[omp:err]"
    try:
        for raw_line in iter(stream.readline, ""):
            chunks.append(raw_line)
            rendered = raw_line.rstrip("\n").strip()
            if on_log_line is not None and rendered:
                on_log_line(f"{prefix} {rendered}")
    except (ValueError, OSError):
        # Stream closed – expected during teardown.
        pass
    finally:
        try:
            stream.close()
        except (ValueError, OSError):
            pass


def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
    """Send SIGTERM to the process group, wait, then SIGKILL.

    Mirrors ``_terminate_agent_process_tree_by_pid`` in ``agent_runner.py``.
    """
    pid = process.pid
    if pid is None:
        return

    # SIGTERM to the whole process group.
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except OSError:
        return

    # Wait up to the grace period for the group to exit.
    deadline = time.monotonic() + _TERMINATE_GRACE_PERIOD_SECONDS
    while time.monotonic() < deadline:
        try:
            os.killpg(pid, 0)
        except ProcessLookupError:
            return
        except OSError:
            break
        time.sleep(0.1)

    # Escalate to SIGKILL.
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, OSError):
        pass


def _build_failure_message(
    *,
    returncode: int,
    stdout: str,
    stderr: str,
) -> str:
    """Compose a human-readable failure summary."""
    source = stderr or stdout or "(no output)"
    # Keep the summary concise – cap at ~2 000 chars.
    if len(source) > 2000:
        source = source[:2000].rstrip() + "..."
    return f"omp exited with code {returncode}: {source}"


def _shell_quote(token: str) -> str:
    """Minimal POSIX shell quoting for safe log display."""
    if not token:
        return "''"
    safe = set(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-+=/:,@"
    )
    if safe.issuperset(token):
        return token
    return "'" + token.replace("'", "'\\''") + "'"
