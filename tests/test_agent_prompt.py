from __future__ import annotations

from app.services.agent_prompt import (
    build_autofix_prompt,
    collect_check_commands,
    summarize_check_results,
)
from app.services.run_hints import OPERATOR_HINTS_PROMPT_PREVIEW_LIMIT


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
        "ci_status": "failed",
        "ci_checks": [
            {
                "source": "workflow_run",
                "name": "CI / unit",
                "status": "completed",
                "conclusion": "failure",
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
    assert "CI status: failed" in prompt
    assert "CI / unit" in prompt
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


def test_build_autofix_prompt_includes_repo_instructions_when_present() -> None:
    prompt = build_autofix_prompt(
        repo="acme/widgets",
        pr_number=24,
        head_sha="abc123def",
        normalized_review={},
        repo_instructions="Do not edit generated files.\nRun pytest before finishing.",
    )

    assert "Repository Instructions (AGENTS.md)" in prompt
    assert "Do not edit generated files." in prompt
    assert "Run pytest before finishing." in prompt


def test_build_autofix_prompt_includes_operator_hints_when_present() -> None:
    prompt = build_autofix_prompt(
        repo="acme/widgets",
        pr_number=24,
        head_sha="abc123def",
        normalized_review={},
        operator_hints="Only touch app/services/filter.py",
    )

    assert "Operator Hints:" in prompt
    assert "Only touch app/services/filter.py" in prompt


def test_build_autofix_prompt_uses_issue_context_for_manual_issue_runs() -> None:
    prompt = build_autofix_prompt(
        repo="acme/widgets",
        pr_number=24,
        head_sha="abc123def",
        normalized_review={
            "source_kind": "issue",
            "issue_number": 11,
            "manual_issue_source_url": "https://github.com/acme/widgets/issues/11",
        },
        pr_metadata={
            "title": "Do not show PR metadata",
            "merge_state_status": "CLEAN",
        },
    )

    assert "manually submitted GitHub issue" in prompt
    assert "- Issue: #11" in prompt
    assert "- Source URL: https://github.com/acme/widgets/issues/11" in prompt
    assert "Pull Request:" not in prompt
    assert "PR Title:" not in prompt


def test_build_autofix_prompt_truncates_operator_hints() -> None:
    prompt = build_autofix_prompt(
        repo="acme/widgets",
        pr_number=24,
        head_sha="abc123def",
        normalized_review={},
        operator_hints="x" * (OPERATOR_HINTS_PROMPT_PREVIEW_LIMIT + 50),
    )

    assert "Operator Hints:" in prompt
    assert ("x" * (OPERATOR_HINTS_PROMPT_PREVIEW_LIMIT + 50)) not in prompt
    assert "..." in prompt


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


def test_build_autofix_prompt_shows_merge_conflict_state() -> None:
    prompt = build_autofix_prompt(
        repo="acme/widgets",
        pr_number=24,
        head_sha="abc123def",
        normalized_review={},
        pr_metadata={
            "title": "Fix bug",
            "merge_state_status": "CONFLICTING",
            "is_merge_conflict": True,
            "can_be_rebased": True,
            "mergeable": False,
        },
    )

    assert "- Merge State: CONFLICTING" in prompt
    assert "⚠️ PR Conflict State:" in prompt
    assert "merge conflicts with the base branch" in prompt
    assert "Automatic merging is not possible" in prompt
    assert "Do not treat the run as complete until the PR is mergeable again." in prompt
    assert "- Can Be Rebased: True" in prompt
    assert "- Mergeable: False" in prompt


def test_build_autofix_prompt_shows_behind_state() -> None:
    prompt = build_autofix_prompt(
        repo="acme/widgets",
        pr_number=24,
        head_sha="abc123def",
        normalized_review={},
        pr_metadata={
            "title": "Feature",
            "merge_state_status": "BEHIND",
            "is_behind": True,
            "is_merge_conflict": False,
            "can_be_rebased": True,
        },
    )

    assert "- Merge State: BEHIND" in prompt
    assert "⚠️ PR Behind Base Branch:" in prompt
    assert "behind the base branch" in prompt
    assert "The run is only complete once the PR is mergeable again." in prompt
    assert "- Can Be Rebased: True" in prompt


def test_build_autofix_prompt_shows_clean_mergeable_state() -> None:
    prompt = build_autofix_prompt(
        repo="acme/widgets",
        pr_number=24,
        head_sha="abc123def",
        normalized_review={},
        pr_metadata={
            "title": "Clean PR",
            "merge_state_status": "MERGEABLE",
            "is_merge_conflict": False,
            "is_behind": False,
            "mergeable": True,
        },
    )

    assert "- Merge State: MERGEABLE" in prompt
    assert "⚠️ PR Conflict State:" not in prompt
    assert "⚠️ PR Behind Base Branch:" not in prompt
    assert "- Mergeable: True" in prompt


def test_build_autofix_prompt_hides_merge_state_when_missing() -> None:
    prompt = build_autofix_prompt(
        repo="acme/widgets",
        pr_number=24,
        head_sha="abc123def",
        normalized_review={},
        pr_metadata={"title": "No merge state"},
    )

    assert "Merge State:" not in prompt
    assert "⚠️ PR Conflict State:" not in prompt
    assert "⚠️ PR Behind Base Branch:" not in prompt
    assert "Can Be Rebased:" not in prompt
    assert "Mergeable:" not in prompt


def test_build_autofix_prompt_conflict_without_rebase_guidance() -> None:
    prompt = build_autofix_prompt(
        repo="acme/widgets",
        pr_number=24,
        head_sha="abc123def",
        normalized_review={},
        pr_metadata={
            "title": "Fix bug",
            "merge_state_status": "CONFLICTING",
            "is_merge_conflict": True,
            "can_be_rebased": False,
        },
    )

    assert "- Merge State: CONFLICTING" in prompt
    assert "⚠️ PR Conflict State:" in prompt
    assert "merge conflicts with the base branch" in prompt
    assert "Automatic merging is not possible" in prompt
    assert "Consider rebasing onto the base branch" not in prompt
    assert "- Can Be Rebased: False" in prompt


def test_build_autofix_prompt_behind_without_rebase_guidance() -> None:
    prompt = build_autofix_prompt(
        repo="acme/widgets",
        pr_number=24,
        head_sha="abc123def",
        normalized_review={},
        pr_metadata={
            "title": "Feature",
            "merge_state_status": "BEHIND",
            "is_behind": True,
            "is_merge_conflict": False,
            "can_be_rebased": False,
        },
    )

    assert "- Merge State: BEHIND" in prompt
    assert "⚠️ PR Behind Base Branch:" in prompt
    assert "behind the base branch" in prompt
    assert "Consider updating the PR branch" in prompt
    assert "can be rebased onto the base branch" not in prompt
    assert "- Can Be Rebased: False" in prompt


def test_build_autofix_prompt_includes_changed_file_paths() -> None:
    prompt = build_autofix_prompt(
        repo="acme/widgets",
        pr_number=24,
        head_sha="abc123def",
        normalized_review={},
        pr_metadata={
            "title": "Feature",
            "changed_file_paths": [
                "app/main.py",
                "app/services/agent.py",
                "tests/test_agent.py",
            ],
        },
    )

    assert "Changed files in this PR:" in prompt
    assert "  - app/main.py" in prompt
    assert "  - app/services/agent.py" in prompt
    assert "  - tests/test_agent.py" in prompt


def test_build_autofix_prompt_truncates_changed_file_paths() -> None:
    from app.services.agent_prompt import CHANGED_FILE_PATHS_LIMIT

    paths = [f"dir/file_{i}.py" for i in range(CHANGED_FILE_PATHS_LIMIT + 10)]
    prompt = build_autofix_prompt(
        repo="acme/widgets",
        pr_number=24,
        head_sha="abc123def",
        normalized_review={},
        pr_metadata={
            "title": "Big PR",
            "changed_file_paths": paths,
        },
    )

    assert "Changed files in this PR:" in prompt
    assert f"  - {paths[0]}" in prompt
    assert f"  - {paths[-1]}" not in prompt
    assert "truncated" in prompt


def test_build_autofix_prompt_omits_changed_file_paths_when_empty() -> None:
    prompt = build_autofix_prompt(
        repo="acme/widgets",
        pr_number=24,
        head_sha="abc123def",
        normalized_review={},
        pr_metadata={
            "title": "Feature",
            "changed_file_paths": [],
        },
    )

    assert "Changed files in this PR:" not in prompt


def test_build_autofix_prompt_omits_changed_file_paths_for_issue_runs() -> None:
    prompt = build_autofix_prompt(
        repo="acme/widgets",
        pr_number=24,
        head_sha="abc123def",
        normalized_review={"source_kind": "issue"},
        pr_metadata={
            "title": "Do not show",
            "changed_file_paths": ["app/main.py"],
        },
    )

    assert "Changed files in this PR:" not in prompt
