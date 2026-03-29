from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator


NonEmptyStr = Annotated[str, Field(min_length=1)]
PositiveInt = Annotated[int, Field(gt=0)]


class IssueSubmissionRequest(BaseModel):
    url: NonEmptyStr
    description: str | None = None
    project_root: str | None = None
    dry_run: bool = False

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    @field_validator("project_root")
    @classmethod
    def validate_project_root(cls, v: str | None) -> str | None:
        if v is None or not v.strip():
            return None
        if v.startswith("/") or ".." in v or ":" in v:
            raise ValueError(
                "project_root must be a relative path without traversal or drive letters"
            )
        return v


class IssueSubmissionResponse(BaseModel):
    ok: bool = True
    message: str
    repo: NonEmptyStr
    pr_number: PositiveInt
    issue_number: int | None = None
    queue_status: str
    queued_run_id: int | None = None
    idempotency_key: str | None = None
    remaining_quota: int | None = None
    head_sha: str | None = None
    existing_run_id: int | None = None
    existing_run_status: str | None = None
