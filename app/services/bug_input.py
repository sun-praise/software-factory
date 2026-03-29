"""Bug input compatibility layer.

Provides a unified abstraction for ingesting bug/fix tasks from multiple
sources (GitHub PRs, GitHub issues, plain text, structured input, logs).

The core flow consumes a *normalized review dict* (the same shape already
stored in ``autofix_runs.normalized_review_json``).  Providers are
responsible for translating their source-specific representation into that
canonical dict so that downstream components (runner, planner, reviewer)
remain agnostic to the origin of the task.

Extending with a new provider
-----------------------------
1. Subclass :class:`BugInputProvider`.
2. Implement :meth:`supports` and :meth:`to_normalized_review`.
3. Call :func:`register_provider` (or use the
   ``BUG_INPUT_PROVIDERS`` list directly) to make the runtime aware of it.
4. The new provider is automatically available via the ``/api/bugs``
   endpoint when its ``provider_kind`` matches the ``BugProviderKind`` enum
   value supplied by the caller.
"""

from __future__ import annotations

import hashlib
import logging
import re
from typing import Any, Protocol, runtime_checkable

from app.schemas.bug_input import BugInput, BugProviderKind


logger = logging.getLogger(__name__)

_MAX_TITLE_LENGTH = 200
_MAX_DESCRIPTION_LENGTH = 50_000

_STACKTRACE_PATTERNS = [
    re.compile(r"Traceback \(most recent call last\):.*?(?=\n\S|\Z)", re.DOTALL),
    re.compile(r"at .+?\(.+?\).*?(?=\n\s*\n|\n\S|\Z)", re.DOTALL),
    re.compile(r"Error:.*", re.DOTALL),
]

_FILE_REFERENCE_PATTERN = re.compile(r"(?:^|\s)([\w./\\-]+\.\w+)(?::(\d+))?(?:$|\s)")


def _build_text_parts(
    title: str, description: str, source_url: str | None
) -> list[str]:
    parts = [f"Title: {title}"]
    if description:
        parts.append(description)
    if source_url:
        parts.append(f"Source: {source_url}")
    return parts


def _build_basic_must_fix_item(
    *,
    source: str,
    bug_input: BugInput,
    text_parts: list[str],
    default_severity: str = "P1",
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "source": source,
        "path": None,
        "line": None,
        "text": "\n\n".join(text_parts),
        "severity": default_severity,
    }
    if bug_input.context.files:
        item["path"] = bug_input.context.files[0]
    if bug_input.context.error_messages and source == "bug_input_plaintext":
        item["severity"] = _classify_severity_from_errors(
            bug_input.context.error_messages
        )
    return item


def _build_review_shell(
    *,
    provider_kind: str,
    repo: str,
    synthetic_pr_number: int,
    must_fix: list[dict[str, Any]],
    title: str,
    source_url: str | None,
) -> dict[str, Any]:
    return {
        "repo": repo,
        "pr_number": synthetic_pr_number,
        "head_sha": None,
        "must_fix": must_fix,
        "should_fix": [],
        "ignore": [],
        "summary": f"{len(must_fix)} blocking issues, 0 suggestions, 0 ignored",
        "project_type": "python",
        "source_kind": "bug_input",
        "bug_provider": provider_kind,
        "bug_title": title[:_MAX_TITLE_LENGTH],
        "bug_source_url": source_url,
    }


@runtime_checkable
class BugInputProvider(Protocol):
    provider_kind: str

    def supports(self, bug_input: BugInput) -> bool: ...
    def to_normalized_review(
        self, bug_input: BugInput, *, repo: str, synthetic_pr_number: int
    ) -> dict[str, Any]: ...


class PlaintextBugProvider:
    provider_kind = "plaintext"

    def supports(self, bug_input: BugInput) -> bool:
        return bug_input.provider == BugProviderKind.PLAINTEXT

    def to_normalized_review(
        self, bug_input: BugInput, *, repo: str, synthetic_pr_number: int
    ) -> dict[str, Any]:
        title = bug_input.title[:_MAX_TITLE_LENGTH]
        text_parts = _build_text_parts(
            title, bug_input.description, bug_input.source_url
        )
        if bug_input.context.files:
            text_parts.append("Referenced files: " + ", ".join(bug_input.context.files))
        if bug_input.context.error_messages:
            text_parts.append(
                "Errors:\n"
                + "\n".join(f"- {e}" for e in bug_input.context.error_messages)
            )
        if bug_input.context.stack_traces:
            text_parts.append(
                "Stack traces:\n" + "\n---\n".join(bug_input.context.stack_traces)
            )
        if bug_input.context.logs:
            text_parts.append("Logs:\n" + "\n---\n".join(bug_input.context.logs[:5]))
        if bug_input.context.metadata:
            meta_lines = [f"{k}: {v}" for k, v in bug_input.context.metadata.items()]
            text_parts.append("Metadata:\n" + "\n".join(meta_lines))

        item = _build_basic_must_fix_item(
            source="bug_input_plaintext",
            bug_input=bug_input,
            text_parts=text_parts,
            default_severity=_classify_severity_from_errors(
                bug_input.context.error_messages
            )
            if bug_input.context.error_messages
            else "P1",
        )
        return _build_review_shell(
            provider_kind=self.provider_kind,
            repo=repo,
            synthetic_pr_number=synthetic_pr_number,
            must_fix=[item],
            title=title,
            source_url=bug_input.source_url,
        )


class GitHubPRBugProvider:
    provider_kind = "github_pr"

    def supports(self, bug_input: BugInput) -> bool:
        return bug_input.provider == BugProviderKind.GITHUB_PR

    def to_normalized_review(
        self, bug_input: BugInput, *, repo: str, synthetic_pr_number: int
    ) -> dict[str, Any]:
        title = bug_input.title[:_MAX_TITLE_LENGTH]
        text_parts = _build_text_parts(
            title, bug_input.description, bug_input.source_url
        )
        item = _build_basic_must_fix_item(
            source="bug_input_github_pr", bug_input=bug_input, text_parts=text_parts
        )
        return _build_review_shell(
            provider_kind=self.provider_kind,
            repo=repo,
            synthetic_pr_number=synthetic_pr_number,
            must_fix=[item],
            title=title,
            source_url=bug_input.source_url,
        )


class GitHubIssueBugProvider:
    provider_kind = "github_issue"

    def supports(self, bug_input: BugInput) -> bool:
        return bug_input.provider == BugProviderKind.GITHUB_ISSUE

    def to_normalized_review(
        self, bug_input: BugInput, *, repo: str, synthetic_pr_number: int
    ) -> dict[str, Any]:
        title = bug_input.title[:_MAX_TITLE_LENGTH]
        text_parts = _build_text_parts(
            title, bug_input.description, bug_input.source_url
        )
        if bug_input.context.error_messages:
            text_parts.append(
                "Errors:\n"
                + "\n".join(f"- {e}" for e in bug_input.context.error_messages)
            )
        item = _build_basic_must_fix_item(
            source="bug_input_github_issue", bug_input=bug_input, text_parts=text_parts
        )
        return _build_review_shell(
            provider_kind=self.provider_kind,
            repo=repo,
            synthetic_pr_number=synthetic_pr_number,
            must_fix=[item],
            title=title,
            source_url=bug_input.source_url,
        )


class StructuredBugProvider:
    provider_kind = "structured"

    def supports(self, bug_input: BugInput) -> bool:
        return bug_input.provider == BugProviderKind.STRUCTURED

    def to_normalized_review(
        self, bug_input: BugInput, *, repo: str, synthetic_pr_number: int
    ) -> dict[str, Any]:
        title = bug_input.title[:_MAX_TITLE_LENGTH]
        ctx = bug_input.context
        must_fix_items = []
        for i, error_msg in enumerate(ctx.error_messages):
            must_fix_items.append(
                {
                    "source": "bug_input_structured",
                    "path": ctx.files[i] if i < len(ctx.files) else None,
                    "line": None,
                    "text": error_msg,
                    "severity": _classify_severity_from_errors([error_msg]),
                }
            )
        for trace, file in zip(ctx.stack_traces, ctx.files[len(ctx.error_messages) :]):
            must_fix_items.append(
                {
                    "source": "bug_input_structured",
                    "path": file,
                    "line": None,
                    "text": trace,
                    "severity": "P0",
                }
            )

        if not must_fix_items:
            description = bug_input.description[:_MAX_DESCRIPTION_LENGTH]
            text_parts = [f"Title: {title}"]
            if description:
                text_parts.append(description)
            must_fix_items.append(
                {
                    "source": "bug_input_structured",
                    "path": ctx.files[0] if ctx.files else None,
                    "line": None,
                    "text": "\n\n".join(text_parts),
                    "severity": "P1",
                }
            )

        return _build_review_shell(
            provider_kind=self.provider_kind,
            repo=repo,
            synthetic_pr_number=synthetic_pr_number,
            must_fix=must_fix_items,
            title=title,
            source_url=bug_input.source_url,
        )


class LogStacktraceBugProvider:
    provider_kind = "log_stacktrace"

    def supports(self, bug_input: BugInput) -> bool:
        return bug_input.provider == BugProviderKind.LOG_STACKTRACE

    def to_normalized_review(
        self, bug_input: BugInput, *, repo: str, synthetic_pr_number: int
    ) -> dict[str, Any]:
        title = bug_input.title[:_MAX_TITLE_LENGTH]
        all_text = bug_input.description[:_MAX_DESCRIPTION_LENGTH]
        if not all_text and bug_input.context.logs:
            all_text = "\n".join(bug_input.context.logs)
        if not all_text and bug_input.context.stack_traces:
            all_text = "\n".join(bug_input.context.stack_traces)
        if not all_text:
            all_text = f"Title: {title}"

        extracted_files = _extract_file_references(all_text)
        extracted_errors = _extract_error_messages(all_text)
        extracted_traces = _extract_stacktraces(all_text)

        text_parts = [f"Title: {title}"]
        if extracted_errors:
            text_parts.append(
                "Detected errors:\n" + "\n".join(f"- {e}" for e in extracted_errors)
            )
        if extracted_files:
            text_parts.append("Referenced files: " + ", ".join(extracted_files[:10]))
        if extracted_traces:
            text_parts.append("Stack traces:\n" + "\n---\n".join(extracted_traces[:3]))
        text_parts.append(f"Raw input:\n{all_text[:_MAX_DESCRIPTION_LENGTH]}")

        must_fix_items = []
        for error in extracted_errors[:5]:
            must_fix_items.append(
                {
                    "source": "bug_input_log_stacktrace",
                    "path": extracted_files[0] if extracted_files else None,
                    "line": None,
                    "text": f"Error: {error}",
                    "severity": _classify_severity_from_errors([error]),
                }
            )
        if not must_fix_items:
            must_fix_items.append(
                {
                    "source": "bug_input_log_stacktrace",
                    "path": extracted_files[0] if extracted_files else None,
                    "line": None,
                    "text": "\n\n".join(text_parts),
                    "severity": "P1",
                }
            )

        ctx = bug_input.context
        for trace, file in zip(ctx.stack_traces, ctx.files):
            must_fix_items.append(
                {
                    "source": "bug_input_log_stacktrace",
                    "path": file,
                    "line": None,
                    "text": trace,
                    "severity": "P0",
                }
            )

        return _build_review_shell(
            provider_kind=self.provider_kind,
            repo=repo,
            synthetic_pr_number=synthetic_pr_number,
            must_fix=must_fix_items,
            title=title,
            source_url=bug_input.source_url,
        )


BUG_INPUT_PROVIDERS: list[BugInputProvider] = [
    PlaintextBugProvider(),
    GitHubPRBugProvider(),
    GitHubIssueBugProvider(),
    StructuredBugProvider(),
    LogStacktraceBugProvider(),
]


def register_provider(provider: BugInputProvider) -> None:
    existing = [
        p for p in BUG_INPUT_PROVIDERS if p.provider_kind == provider.provider_kind
    ]
    if existing:
        BUG_INPUT_PROVIDERS.remove(existing[0])
    BUG_INPUT_PROVIDERS.append(provider)
    logger.info("Registered bug input provider: %s", provider.provider_kind)


def resolve_provider(bug_input: BugInput) -> BugInputProvider:
    for provider in BUG_INPUT_PROVIDERS:
        if provider.supports(bug_input):
            return provider
    return PlaintextBugProvider()


def build_bug_idempotency_key(*, repo: str, title: str, source_url: str | None) -> str:
    raw = f"bug:{repo}:{title}:{source_url or ''}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def _classify_severity_from_errors(errors: list[str]) -> str:
    combined = " ".join(errors).lower()
    if any(kw in combined for kw in ("security", "critical", "crash", "data loss")):
        return "P0"
    if any(kw in combined for kw in ("exception", "error", "fail", "timeout")):
        return "P1"
    if any(kw in combined for kw in ("warning", "deprecat")):
        return "P2"
    return "P3"


def _extract_file_references(text: str) -> list[str]:
    matches = _FILE_REFERENCE_PATTERN.findall(text)
    return list(
        dict.fromkeys(m[0] for m in matches if m[0] and not m[0].startswith("/"))
    )


def _extract_error_messages(text: str) -> list[str]:
    lines = text.splitlines()
    errors: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        lower = stripped.lower()
        if any(
            kw in lower
            for kw in ("error:", "error:", "exception:", "failed", "failure")
        ):
            errors.append(stripped)
    return errors[:20]


def _extract_stacktraces(text: str) -> list[str]:
    traces: list[str] = []
    for pattern in _STACKTRACE_PATTERNS:
        for match in pattern.finditer(text):
            trace = match.group(0).strip()
            if len(trace) > 10:
                traces.append(trace[:2000])
    return list(dict.fromkeys(traces))[:5]
