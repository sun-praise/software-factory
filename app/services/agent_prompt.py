from __future__ import annotations

from typing import Any, Mapping


PR_BODY_PREVIEW_LIMIT = 600


def build_autofix_prompt(
    repo: str,
    pr_number: int,
    head_sha: str,
    normalized_review: Mapping[str, Any],
    pr_metadata: Mapping[str, Any] | None = None,
) -> str:
    must_fix = _as_issue_list(normalized_review.get("must_fix"))
    should_fix = _as_issue_list(normalized_review.get("should_fix"))
    metadata = pr_metadata or {}

    must_fix_summary = _format_issue_summary("must_fix", must_fix)
    should_fix_summary = _format_issue_summary("should_fix", should_fix)

    lines = [
        "You are an autofix agent working on a pull request.",
        "",
        "Context:",
        f"- Repository: {repo}",
        f"- Pull Request: #{pr_number}",
        f"- Head SHA: {head_sha}",
    ]
    _append_pr_metadata(lines, metadata)
    lines.extend(
        [
            "",
            "Hard constraints:",
            "- Only fix issues explicitly listed in review feedback.",
            "- Do not perform unrelated refactors.",
            "- Do not expand the scope of changes beyond touched files/lines that are required for the listed issues.",
            "- Prioritize passing existing tests before any optional improvement.",
            "- If a required fix cannot be completed, output the reason and stop.",
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


def _append_pr_metadata(lines: list[str], metadata: Mapping[str, Any]) -> None:
    title = _safe_text(metadata.get("title"), "")
    base_ref = _safe_text(metadata.get("base_ref"), "")
    head_ref = _safe_text(metadata.get("head_ref"), "")
    changed_files = _positive_int_text(metadata.get("changed_files"))
    additions = _positive_int_text(metadata.get("additions"))
    deletions = _positive_int_text(metadata.get("deletions"))
    body = _safe_text(metadata.get("body"), "")

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
    if body:
        compact_body = " ".join(body.split())
        if len(compact_body) > PR_BODY_PREVIEW_LIMIT:
            compact_body = f"{compact_body[:PR_BODY_PREVIEW_LIMIT].rstrip()}..."
        lines.append(f"- PR Body: {compact_body}")


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
