from __future__ import annotations

import pytest

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


@pytest.mark.parametrize("raw", ["", "   \n\t  "])
def test_parse_execution_hints_returns_empty_for_blank_text(raw: str) -> None:
    hints = parse_execution_hints(raw)

    assert hints.project_root is None
    assert hints.check_commands == ()
    assert hints.skip_baseline_checks is False


def test_parse_execution_hints_is_case_insensitive_and_ignores_blank_commands() -> None:
    hints = parse_execution_hints(
        "\n".join(
            [
                "Project_Root: latex-agent",
                "CHECK-COMMAND: python -m pytest -q",
                "check_command:   ",
                "Skip-Baseline-Checks: YES",
            ]
        )
    )

    assert hints.project_root == "latex-agent"
    assert hints.check_commands == ("python -m pytest -q",)
    assert hints.skip_baseline_checks is True


def test_parse_execution_hints_last_project_root_wins_and_commands_accumulate() -> None:
    hints = parse_execution_hints(
        "\n".join(
            [
                "project_root: frontend",
                "check_command: npm test",
                "project_root: backend",
                "check_command: python -m pytest -q",
            ]
        )
    )

    assert hints.project_root == "backend"
    assert hints.check_commands == ("npm test", "python -m pytest -q")
