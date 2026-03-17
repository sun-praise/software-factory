from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field


NonEmptyStr = Annotated[str, Field(min_length=1)]
PositiveInt = Annotated[int, Field(gt=0)]


class IssueSubmissionRequest(BaseModel):
    url: NonEmptyStr
    description: str | None = None

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


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
