from __future__ import annotations

from app.services.run_hints import parse_execution_hints


def test_parse_execution_hints_extracts_structured_values() -> None:
    hints = parse_execution_hints(
        "\n".join(
            [
                "project_root: latex-agent",
                "check_command: python -m pytest -q",
                "check_command: python -m ruff check .",
                "skip_baseline_checks: true",
                "Only touch backend files.",
            ]
        )
    )

    assert hints.project_root == "latex-agent"
    assert hints.check_commands == (
        "python -m pytest -q",
        "python -m ruff check .",
    )
    assert hints.skip_baseline_checks is True


def test_parse_execution_hints_ignores_non_structured_lines() -> None:
    hints = parse_execution_hints("Only touch latex-agent/\nDo not edit frontend.")

    assert hints.project_root is None
    assert hints.check_commands == ()
    assert hints.skip_baseline_checks is False
