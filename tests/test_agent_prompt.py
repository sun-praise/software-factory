from __future__ import annotations

from app.services.agent_prompt import (
    build_autofix_prompt,
    collect_check_commands,
    summarize_check_results,
)


def test_build_autofix_prompt_contains_required_constraints_and_summaries() -> None:
    normalized_review = {
        "must_fix": [
            {
                "source": "pull_request_review_comment",
                "path": "app/main.py",
                "line": 42,
                "text": "Handle None case to avoid exception",
            }
        ],
        "should_fix": [
            {
                "source": "issue_comment",
                "path": None,
                "line": None,
                "text": "Consider improving message clarity",
            }
        ],
    }

    prompt = build_autofix_prompt(
        repo="acme/widgets",
        pr_number=24,
        head_sha="abc123def",
        normalized_review=normalized_review,
    )

    assert "Only fix issues explicitly listed in review feedback." in prompt
    assert "Do not perform unrelated refactors." in prompt
    assert "Do not expand the scope of changes" in prompt
    assert "Prioritize passing existing tests" in prompt
    assert "output the reason and stop" in prompt
    assert "must_fix" in prompt
    assert "should_fix" in prompt
    assert "acme/widgets" in prompt
    assert "#24" in prompt


def test_build_autofix_prompt_hides_zero_value_pr_stats() -> None:
    prompt = build_autofix_prompt(
        repo="acme/widgets",
        pr_number=24,
        head_sha="abc123def",
        normalized_review={},
        pr_metadata={
            "changed_files": 0,
            "additions": 0,
            "deletions": 0,
        },
    )

    assert "Changed Files:" not in prompt
    assert "Diff Stats:" not in prompt


def test_build_autofix_prompt_shows_positive_pr_stats() -> None:
    prompt = build_autofix_prompt(
        repo="acme/widgets",
        pr_number=24,
        head_sha="abc123def",
        normalized_review={},
        pr_metadata={
            "changed_files": 3,
            "additions": "5",
            "deletions": 2,
        },
    )

    assert "- Changed Files: 3" in prompt
    assert "- Diff Stats: +5 / -2" in prompt


def test_collect_check_commands_defaults_to_python_commands() -> None:
    assert collect_check_commands() == [
        "python -m pytest -q",
        "python -m ruff check .",
        "python -m mypy .",
    ]


def test_collect_check_commands_unknown_type_returns_empty() -> None:
    assert collect_check_commands("elixir") == []


def test_collect_check_commands_for_python_node_go_rust() -> None:
    assert collect_check_commands("python") == [
        "python -m pytest -q",
        "python -m ruff check .",
        "python -m mypy .",
    ]
    assert collect_check_commands("node") == [
        "npm test -- --runInBand",
        "npm run lint",
        "npm run typecheck",
    ]
    assert collect_check_commands("go") == [
        "go test ./...",
        "go vet ./...",
        "go test ./... -run ^$",
    ]
    assert collect_check_commands("rust") == [
        "cargo test --quiet",
        "cargo clippy --all-targets -- -D warnings",
        "cargo check --all-targets",
    ]


def test_summarize_check_results_all_passed() -> None:
    results = [
        {
            "command": "python -m pytest -q",
            "exit_code": 0,
            "stdout": "ok",
            "stderr": "",
        },
        {
            "command": "python -m ruff check .",
            "exit_code": 0,
            "stdout": "",
            "stderr": "",
        },
    ]

    summary = summarize_check_results(results)

    assert summary == {
        "overall_status": "passed",
        "passed_count": 2,
        "failed_count": 0,
        "failed_commands": [],
    }


def test_summarize_check_results_with_failures() -> None:
    results = [
        {"command": "npm test", "exit_code": 1, "stdout": "", "stderr": "failed"},
        {"command": "npm run lint", "exit_code": 0, "stdout": "", "stderr": ""},
        {
            "command": "npm run typecheck",
            "exit_code": 2,
            "stdout": "",
            "stderr": "errors",
        },
    ]

    summary = summarize_check_results(results)

    assert summary["overall_status"] == "failed"
    assert summary["passed_count"] == 1
    assert summary["failed_count"] == 2
    assert summary["failed_commands"] == ["npm test", "npm run typecheck"]
