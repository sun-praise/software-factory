from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


NonEmptyStr = Annotated[str, Field(min_length=1)]
PositiveInt = Annotated[int, Field(gt=0)]
Severity = Literal["P0", "P1", "P2", "P3"]


class IssueSubmissionRequest(BaseModel):
    repo: NonEmptyStr
    pr_number: PositiveInt
    issue_number: PositiveInt | None = None
    title: NonEmptyStr
    body: NonEmptyStr
    head_sha: NonEmptyStr | None = None
    branch: NonEmptyStr | None = None
    priority: Literal["must_fix", "should_fix"] = "must_fix"
    severity: Severity = "P1"
    project_type: NonEmptyStr | None = None

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class IssueSubmissionResponse(BaseModel):
    ok: bool = True
    message: str
    repo: NonEmptyStr
    pr_number: int
    issue_number: int | None = None
    queue_status: str
    queued_run_id: int | None = None
    idempotency_key: str | None = None
    remaining_quota: int | None = None
    head_sha: str | None = None
