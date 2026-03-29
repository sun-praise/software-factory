from __future__ import annotations

from enum import Enum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field


NonEmptyStr = Annotated[str, Field(min_length=1)]


class BugProviderKind(str, Enum):
    PLAINTEXT = "plaintext"
    GITHUB_PR = "github_pr"
    GITHUB_ISSUE = "github_issue"
    STRUCTURED = "structured"
    LOG_STACKTRACE = "log_stacktrace"


class BugContext(BaseModel):
    files: list[str] = Field(default_factory=list)
    error_messages: list[str] = Field(default_factory=list)
    stack_traces: list[str] = Field(default_factory=list)
    logs: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(extra="allow", str_strip_whitespace=True)


class BugInput(BaseModel):
    provider: BugProviderKind = BugProviderKind.PLAINTEXT
    title: NonEmptyStr
    description: str = ""
    repo: str | None = None
    source_url: str | None = None
    context: BugContext = Field(default_factory=BugContext)

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class BugSubmissionRequest(BaseModel):
    provider: BugProviderKind = BugProviderKind.PLAINTEXT
    title: NonEmptyStr
    description: str = ""
    repo: str | None = None
    source_url: str | None = None
    context: BugContext | None = None
    dry_run: bool = False

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    def to_bug_input(self) -> BugInput:
        return BugInput(
            **self.model_dump(exclude={"dry_run", "context"}),
            context=self.context or BugContext(),
        )


class BugSubmissionResponse(BaseModel):
    ok: bool = True
    message: str
    repo: str | None = None
    queue_status: str
    queued_run_id: int | None = None
    idempotency_key: str | None = None
    remaining_quota: int | None = None
    head_sha: str | None = None
