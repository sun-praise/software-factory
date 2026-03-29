from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlparse


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TaskInput:
    title: str
    body: str
    provider: str
    source_url: str | None = None
    source_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def display_label(self) -> str:
        if self.source_url:
            return self.source_url
        if self.source_id:
            return f"{self.provider}#{self.source_id}"
        return f"{self.provider}: {self.title}"


@runtime_checkable
class IssueProvider(Protocol):
    provider_name: str

    def can_handle(self, raw_input: str) -> bool: ...

    def parse(self, raw_input: str) -> TaskInput: ...


class GitHubIssueProvider:
    provider_name: str = "github"

    def can_handle(self, raw_input: str) -> bool:
        stripped = raw_input.strip()
        if not stripped:
            return False
        try:
            parsed = urlparse(stripped)
            if parsed.hostname and parsed.hostname.lower() == "github.com":
                path_parts = [p for p in parsed.path.split("/") if p]
                if len(path_parts) >= 4 and path_parts[2] in (
                    "issues",
                    "pull",
                    "pulls",
                ):
                    return True
        except Exception:
            pass
        return False

    def parse(self, raw_input: str) -> TaskInput:
        stripped = raw_input.strip()
        parsed = urlparse(stripped)
        path_parts = [p for p in parsed.path.split("/") if p]
        if len(path_parts) < 4:
            raise ValueError(
                "Invalid GitHub URL. Expected: "
                "https://github.com/<owner>/<repo>/issues/<number> or "
                "https://github.com/<owner>/<repo>/pull/<number>"
            )
        owner, repo_name = path_parts[0], path_parts[1]
        section = path_parts[2]
        number_part = path_parts[3]
        try:
            number = int(number_part)
        except ValueError:
            raise ValueError("GitHub URL must contain a numeric issue or PR number.")

        repo = f"{owner}/{repo_name}"
        return TaskInput(
            title=f"{repo} {section} #{number}",
            body=stripped,
            provider=self.provider_name,
            source_url=stripped,
            source_id=f"{repo}/{section}/{number}",
            metadata={
                "repo": repo,
                "owner": owner,
                "repo_name": repo_name,
                "section": section,
                "number": number,
                "url_kind": "pull" if section in ("pull", "pulls") else "issue",
            },
        )


class PlainTextProvider:
    provider_name: str = "plain_text"

    def can_handle(self, raw_input: str) -> bool:
        return len(raw_input.strip()) > 0

    def parse(self, raw_input: str) -> TaskInput:
        stripped = raw_input.strip()
        lines = stripped.split("\n", 1)
        title = lines[0].strip()
        body = lines[1].strip() if len(lines) > 1 else title
        if not title:
            raise ValueError("Plain text input must not be empty.")
        return TaskInput(
            title=title,
            body=body,
            provider=self.provider_name,
            source_id=None,
            source_url=None,
            metadata={"raw_text": stripped},
        )


_PROVIDER_REGISTRY: list[IssueProvider] = [
    GitHubIssueProvider(),
    PlainTextProvider(),
]


def register_provider(provider: IssueProvider) -> None:
    for existing in _PROVIDER_REGISTRY:
        if existing.provider_name == provider.provider_name:
            logger.warning("Overriding existing provider: %s", provider.provider_name)
            _PROVIDER_REGISTRY.remove(existing)
            break
    _PROVIDER_REGISTRY.append(provider)
    logger.info("Registered issue provider: %s", provider.provider_name)


def get_registered_providers() -> list[str]:
    return [p.provider_name for p in _PROVIDER_REGISTRY]


def parse_task_input(raw_input: str) -> TaskInput:
    stripped = raw_input.strip()
    if not stripped:
        raise ValueError("Input must not be empty.")

    for provider in _PROVIDER_REGISTRY:
        if provider.can_handle(stripped):
            try:
                return provider.parse(stripped)
            except ValueError:
                continue

    return PlainTextProvider().parse(stripped)


def parse_task_input_with_provider(raw_input: str, provider_name: str) -> TaskInput:
    stripped = raw_input.strip()
    if not stripped:
        raise ValueError("Input must not be empty.")

    for provider in _PROVIDER_REGISTRY:
        if provider.provider_name == provider_name:
            if not provider.can_handle(stripped):
                raise ValueError(
                    f"Provider '{provider_name}' cannot handle this input."
                )
            return provider.parse(stripped)

    available = ", ".join(get_registered_providers())
    raise ValueError(f"Unknown provider '{provider_name}'. Available: {available}")


def build_normalized_review_from_task_input(
    *,
    task_input: TaskInput,
    repo: str | None = None,
    description: str | None = None,
    head_sha: str | None = None,
    pr_number: int | None = None,
) -> dict[str, Any]:
    parts: list[str] = []
    parts.append(f"Task: {task_input.title}")
    parts.append(f"Provider: {task_input.provider}")
    if task_input.source_url:
        parts.append(f"Source URL: {task_input.source_url}")
    if task_input.source_id:
        parts.append(f"Source ID: {task_input.source_id}")
    if description:
        parts.append(f"Operator note:\n{description}")
    parts.append(f"Task body:\n{task_input.body}")

    issue_text = "\n\n".join(part for part in parts if part)

    github_metadata = task_input.metadata.get("repo")
    effective_repo = repo or github_metadata or "unknown/unknown"
    effective_pr_number = pr_number
    if effective_pr_number is None:
        num = task_input.metadata.get("number")
        if isinstance(num, int) and num > 0:
            effective_pr_number = num

    if effective_pr_number is None:
        effective_pr_number = 0

    item = {
        "source": f"task_input:{task_input.provider}",
        "path": None,
        "line": None,
        "text": issue_text,
        "severity": "P1",
        "source_url": task_input.source_url,
        "task_provider": task_input.provider,
        "task_title": task_input.title,
        "context_resolved": True,
    }

    must_fix: list[dict[str, Any]] = [item]
    should_fix: list[dict[str, Any]] = []

    result: dict[str, Any] = {
        "repo": effective_repo,
        "pr_number": effective_pr_number,
        "head_sha": head_sha,
        "must_fix": must_fix,
        "should_fix": should_fix,
        "ignore": [],
        "summary": f"{len(must_fix)} blocking issues, {len(should_fix)} suggestions, 0 ignored",
        "project_type": "python",
        "source_kind": task_input.provider,
        "task_input_provider": task_input.provider,
        "task_input_source_url": task_input.source_url,
        "task_input_source_id": task_input.source_id,
    }
    return result
