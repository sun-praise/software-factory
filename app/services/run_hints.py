from __future__ import annotations

from dataclasses import dataclass


RUN_HINT_EDITABLE_STATUSES = frozenset(
    {"queued", "running", "cancel_requested", "retry_scheduled"}
)
OPERATOR_HINT_SEPARATOR = "\n\n---\n"
OPERATOR_HINT_APPEND_MAX_CHARS = 1_000
OPERATOR_HINTS_MAX_CHARS = 4_000
OPERATOR_HINTS_PROMPT_PREVIEW_LIMIT = 2_000
EXECUTION_HINT_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


@dataclass(frozen=True)
class ExecutionHints:
    """Structured execution overrides parsed from operator hints text."""

    project_root: str | None = None
    check_commands: tuple[str, ...] = ()
    skip_baseline_checks: bool = False


def parse_execution_hints(text: str | None) -> ExecutionHints:
    """Extract operator-provided execution overrides from free-form hints text.

    Supported lines are:
    - ``project_root: relative/path``
    - ``check_command: python -m pytest -q``
    - ``skip_baseline_checks: true``

    Parsing is intentionally permissive: blank ``check_command`` lines are ignored,
    and path validation happens later in the runner where workspace containment can
    be checked against the actual execution directory.
    """

    project_root: str | None = None
    check_commands: list[str] = []
    skip_baseline_checks = False

    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line or ":" not in line:
            continue
        key, _, value = line.partition(":")
        normalized_key = key.strip().lower().replace("-", "_")
        normalized_value = value.strip()
        if not normalized_value:
            continue
        if normalized_key == "project_root":
            project_root = normalized_value
        elif normalized_key == "check_command":
            check_commands.append(normalized_value)
        elif normalized_key == "skip_baseline_checks":
            skip_baseline_checks = normalized_value.lower() in EXECUTION_HINT_TRUE_VALUES

    return ExecutionHints(
        project_root=project_root,
        check_commands=tuple(check_commands),
        skip_baseline_checks=skip_baseline_checks,
    )
