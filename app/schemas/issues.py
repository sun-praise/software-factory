from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


NonEmptyStr = Annotated[str, Field(min_length=1)]
PositiveInt = Annotated[int, Field(gt=0)]


class IssueSubmissionRequest(BaseModel):
    url: NonEmptyStr | None = None
    repo: NonEmptyStr | None = None
    title: str | None = None
    text: str | None = None
    description: str | None = None
    project_root: str | None = Field(default=None, max_length=256)
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

    @model_validator(mode="after")
    def validate_source(self) -> "IssueSubmissionRequest":
        if self.url:
            if self.repo or self.title or self.text:
                raise ValueError(
                    "Provide either `url`, or `repo` + `text`, but not both."
                )
            return self
        if not self.repo or not self.text:
            raise ValueError("Provide either `url`, or both `repo` and `text`.")
        return self


class IssueSubmissionResponse(BaseModel):
    ok: bool = True
    message: str
    repo: NonEmptyStr
    pr_number: PositiveInt | None = None
    issue_number: int | None = None
    source_kind: NonEmptyStr
    source_ref: str | None = None
    queue_status: str
    queued_run_id: int | None = None
    idempotency_key: str | None = None
    remaining_quota: int | None = None
    head_sha: str | None = None
    existing_run_id: int | None = None
    existing_run_status: str | None = None
