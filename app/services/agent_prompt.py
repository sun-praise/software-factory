from __future__ import annotations

from typing import Any, Mapping

from app.services.run_hints import OPERATOR_HINTS_PROMPT_PREVIEW_LIMIT


PR_BODY_PREVIEW_LIMIT = 600
REPO_INSTRUCTIONS_PREVIEW_LIMIT = 4_000
CHANGED_FILE_PATHS_LIMIT = 50


def build_autofix_prompt(
    repo: str,
    pr_number: int,
    head_sha: str,
    normalized_review: Mapping[str, Any],
    pr_metadata: Mapping[str, Any] | None = None,
    repo_instructions: str | None = None,
    operator_hints: str | None = None,
) -> str:
    must_fix = _as_issue_list(normalized_review.get("must_fix"))
    should_fix = _as_issue_list(normalized_review.get("should_fix"))
    metadata = pr_metadata or {}
    ci_checks = _as_ci_check_list(normalized_review.get("ci_checks"))

    must_fix_summary = _format_issue_summary("must_fix", must_fix)
    should_fix_summary = _format_issue_summary("should_fix", should_fix)
    ci_summary = _format_ci_summary(
        ci_status=_safe_text(normalized_review.get("ci_status"), "unknown"),
        ci_checks=ci_checks,
    )

    lines = _build_run_context_lines(
        repo=repo,
        pr_number=pr_number,
        head_sha=head_sha,
        normalized_review=normalized_review,
    )
    if not _is_issue_sourced_run(normalized_review):
        _append_pr_merge_state_context(lines, metadata)
        _append_pr_metadata(lines, metadata)
    _append_repo_instructions(lines, repo_instructions)
    _append_operator_hints(lines, operator_hints)
    lines.extend(
        [
            ci_summary,
            "",
            "Hard constraints:",
            "- Only fix issues explicitly listed in review feedback.",
            "- Do not perform unrelated refactors.",
            "- Do not expand the scope of changes beyond touched files/lines that are required for the listed issues.",
            "- Prioritize passing existing tests before any optional improvement.",
            "- If a required fix cannot be completed, output the reason and stop.",
            "- Treat CI failures as supporting context, not as permission for unrelated changes.",
            "",
            "Work items:",
            must_fix_summary,
            should_fix_summary,
            "",
            "Execution policy:",
            "- Apply must_fix items first.",
            "- Apply should_fix items only if they do not risk breaking tests.",
            "- Keep patches minimal and directly traceable to review comments.",
        ]
    )
    return "\n".join(lines)


def _build_run_context_lines(
    *,
    repo: str,
    pr_number: int,
    head_sha: str,
    normalized_review: Mapping[str, Any],
) -> list[str]:
    if _is_issue_sourced_run(normalized_review):
        lines = [
            "You are an autofix agent working on a manually submitted GitHub issue.",
            "",
            "Context:",
            f"- Repository: {repo}",
        ]
        issue_number = _safe_text(normalized_review.get("issue_number"), "")
        if issue_number:
            lines.append(f"- Issue: #{issue_number}")
        source_url = _safe_text(normalized_review.get("manual_issue_source_url"), "")
        if source_url:
            lines.append(f"- Source URL: {source_url}")
        lines.append(f"- Head SHA: {head_sha}")
        return lines

    return [
        "You are an autofix agent working on a pull request.",
        "",
        "Context:",
        f"- Repository: {repo}",
        f"- Pull Request: #{pr_number}",
        f"- Head SHA: {head_sha}",
    ]


def _is_issue_sourced_run(normalized_review: Mapping[str, Any]) -> bool:
    return _safe_text(normalized_review.get("source_kind"), "").lower() == "issue"


def collect_check_commands(project_type: str | None = None) -> list[str]:
    templates = {
        "python": [
            "python -m pytest -q",
            "python -m ruff check .",
            "python -m mypy .",
        ],
        "node": [
            "npm test -- --runInBand",
            "npm run lint",
            "npm run typecheck",
        ],
        "go": [
            "go test ./...",
            "go vet ./...",
            "go test ./... -run ^$",
        ],
        "rust": [
            "cargo test --quiet",
            "cargo clippy --all-targets -- -D warnings",
            "cargo check --all-targets",
        ],
    }

    normalized = (project_type or "").strip().lower()
    if not normalized:
        return templates["python"]

    if normalized in templates:
        return templates[normalized]

    return []


def summarize_check_results(results: list[dict[str, Any]]) -> dict[str, Any]:
    passed_count = 0
    failed_count = 0
    failed_commands: list[str] = []

    for result in results:
        command = str(result.get("command", ""))
        exit_code = result.get("exit_code")
        if isinstance(exit_code, int) and exit_code == 0:
            passed_count += 1
            continue
        failed_count += 1
        failed_commands.append(command)

    overall_status = "passed" if failed_count == 0 else "failed"
    return {
        "overall_status": overall_status,
        "passed_count": passed_count,
        "failed_count": failed_count,
        "failed_commands": failed_commands,
    }


def _as_issue_list(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        return []
    issue_items: list[Mapping[str, Any]] = []
    for item in value:
        if isinstance(item, Mapping):
            issue_items.append(item)
    return issue_items


def _as_ci_check_list(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        return []
    check_items: list[Mapping[str, Any]] = []
    for item in value:
        if isinstance(item, Mapping):
            check_items.append(item)
    return check_items


def _format_issue_summary(title: str, items: list[Mapping[str, Any]]) -> str:
    if not items:
        return f"- {title}: 0 items"

    formatted_items: list[str] = []
    for item in items:
        source = _safe_text(item.get("source"), "unknown")
        path = _safe_text(item.get("path"), "n/a")
        line = _safe_text(item.get("line"), "n/a")
        text = _safe_text(item.get("text"), "")
        formatted_items.append(f"[{source}] {path}:{line} {text}".strip())

    joined = " | ".join(formatted_items)
    return f"- {title}: {len(items)} items -> {joined}"


def _append_pr_merge_state_context(
    lines: list[str], metadata: Mapping[str, Any]
) -> None:
    is_merge_conflict = metadata.get("is_merge_conflict")
    is_behind = metadata.get("is_behind")
    can_be_rebased = metadata.get("can_be_rebased")

    if not is_merge_conflict and not is_behind:
        return

    if is_merge_conflict:
        lines.extend(
            [
                "",
                "⚠️ PR Conflict State:",
                "- This pull request has merge conflicts with the base branch.",
                "- Automatic merging is not possible until conflicts are resolved.",
                "- Do not treat the run as complete until the PR is mergeable again.",
            ]
        )
        if can_be_rebased:
            lines.extend(
                [
                    "- The PR can be rebased. Consider rebasing onto the base branch to resolve conflicts.",
                ]
            )
        lines.append("")
        return

    if is_behind:
        lines.extend(
            [
                "",
                "⚠️ PR Behind Base Branch:",
                "- This pull request is behind the base branch.",
                "- Consider updating the PR branch before applying fixes.",
                "- The run is only complete once the PR is mergeable again.",
            ]
        )
        if can_be_rebased:
            lines.extend(
                [
                    "- The PR can be rebased onto the base branch.",
                ]
            )
        lines.append("")


def _append_pr_metadata(lines: list[str], metadata: Mapping[str, Any]) -> None:
    title = _safe_text(metadata.get("title"), "")
    base_ref = _safe_text(metadata.get("base_ref"), "")
    head_ref = _safe_text(metadata.get("head_ref"), "")
    changed_files = _positive_int_text(metadata.get("changed_files"))
    additions = _positive_int_text(metadata.get("additions"))
    deletions = _positive_int_text(metadata.get("deletions"))
    body = _safe_text(metadata.get("body"), "")
    merge_state_status = _safe_text(metadata.get("merge_state_status"), "")
    can_be_rebased = metadata.get("can_be_rebased")
    mergeable = metadata.get("mergeable")

    if title:
        lines.append(f"- PR Title: {title}")
    if base_ref:
        lines.append(f"- Base Ref: {base_ref}")
    if head_ref:
        lines.append(f"- Head Ref: {head_ref}")
    if changed_files:
        lines.append(f"- Changed Files: {changed_files}")
    if additions or deletions:
        lines.append(f"- Diff Stats: +{additions or '0'} / -{deletions or '0'}")
    if merge_state_status:
        lines.append(f"- Merge State: {merge_state_status}")
    if can_be_rebased is not None:
        lines.append(f"- Can Be Rebased: {can_be_rebased}")
    if mergeable is not None:
        lines.append(f"- Mergeable: {mergeable}")
    if body:
        compact_body = " ".join(body.split())
        if len(compact_body) > PR_BODY_PREVIEW_LIMIT:
            compact_body = f"{compact_body[:PR_BODY_PREVIEW_LIMIT].rstrip()}..."
        lines.append(f"- PR Body: {compact_body}")
    _append_changed_file_paths(lines, metadata)


def _append_changed_file_paths(lines: list[str], metadata: Mapping[str, Any]) -> None:
    raw_paths = metadata.get("changed_file_paths")
    if not isinstance(raw_paths, list) or not raw_paths:
        return
    paths = [str(p).strip() for p in raw_paths if str(p).strip()]
    if not paths:
        return
    lines.extend(
        ["", "Changed files in this PR:"]
        + [f"  - {p}" for p in paths[:CHANGED_FILE_PATHS_LIMIT]]
    )
    if len(paths) > CHANGED_FILE_PATHS_LIMIT:
        lines.append(
            f"  ... and {len(paths) - CHANGED_FILE_PATHS_LIMIT} more (truncated)"
        )


def _format_ci_summary(ci_status: str, ci_checks: list[Mapping[str, Any]]) -> str:
    if not ci_checks:
        return "- CI status: unknown (no CI checks captured)"

    formatted_checks: list[str] = []
    for item in ci_checks[:6]:
        source = _safe_text(item.get("source"), "unknown")
        name = _safe_text(item.get("name"), "unnamed")
        status = _safe_text(item.get("status"), "unknown")
        conclusion = _safe_text(item.get("conclusion"), "unknown")
        formatted_checks.append(
            f"[{source}] {name} => status={status}, conclusion={conclusion}"
        )
    joined = " | ".join(formatted_checks)
    return f"- CI status: {ci_status} -> {joined}"


def _append_repo_instructions(lines: list[str], repo_instructions: str | None) -> None:
    instructions = _safe_text(repo_instructions, "")
    if not instructions:
        return
    compact = instructions.strip()
    if len(compact) > REPO_INSTRUCTIONS_PREVIEW_LIMIT:
        compact = f"{compact[:REPO_INSTRUCTIONS_PREVIEW_LIMIT].rstrip()}..."
    lines.extend(
        [
            "- Repository Instructions (AGENTS.md):",
            compact,
        ]
    )


def _append_operator_hints(lines: list[str], operator_hints: str | None) -> None:
    hints = _safe_text(operator_hints, "")
    if not hints:
        return
    compact = hints.strip()
    if len(compact) > OPERATOR_HINTS_PROMPT_PREVIEW_LIMIT:
        compact = f"{compact[:OPERATOR_HINTS_PROMPT_PREVIEW_LIMIT].rstrip()}..."
    lines.extend(
        [
            "- Operator Hints:",
            compact,
        ]
    )


def _safe_text(value: Any, fallback: str) -> str:
    if value is None:
        return fallback
    text = str(value).strip()
    return text if text else fallback


def _positive_int_text(value: Any) -> str:
    if isinstance(value, bool) or value is None:
        return ""
    try:
        normalized = int(str(value).strip())
    except (TypeError, ValueError):
        return ""
    if normalized <= 0:
        return ""
    return str(normalized)
