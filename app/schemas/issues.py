from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field


NonEmptyStr = Annotated[str, Field(min_length=1)]


class IssueSubmissionRequest(BaseModel):
    url: NonEmptyStr
    description: str | None = None

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class IssueSubmissionResponse(BaseModel):
    ok: bool = True
    message: str
    repo: NonEmptyStr
    pr_number: int | None = None
    issue_number: int | None = None
    source_kind: NonEmptyStr
    queue_status: str
    queued_run_id: int | None = None
    idempotency_key: str | None = None
    remaining_quota: int | None = None
    head_sha: str | None = None
    base_branch: str | None = None
    working_branch: str | None = None
