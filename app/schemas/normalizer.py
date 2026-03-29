from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


NonEmptyStr = Annotated[str, Field(min_length=1)]
PositiveInt = Annotated[int, Field(gt=0)]


class IssueItem(BaseModel):
    source: NonEmptyStr
    path: str | None = None
    line: PositiveInt | None = None
    severity: Literal["P0", "P1", "P2", "P3"]
    text: NonEmptyStr
    semantic_type: Literal[
        "blocking_defect",
        "non_blocking_suggestion",
        "clarification",
        "informational",
        "needs_human_decision",
    ] = "non_blocking_suggestion"
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    needs_human_review: bool = False
    group_id: str | None = None

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class CICheckItem(BaseModel):
    source: NonEmptyStr
    name: NonEmptyStr
    status: NonEmptyStr
    conclusion: NonEmptyStr
    details_url: str | None = None
    head_sha: str | None = None

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class NormalizedReview(BaseModel):
    repo: NonEmptyStr
    pr_number: PositiveInt
    head_sha: NonEmptyStr | None = None
    review_batch_id: NonEmptyStr | None = None
    must_fix: list[IssueItem] = Field(default_factory=list)
    should_fix: list[IssueItem] = Field(default_factory=list)
    ignore: list[IssueItem] = Field(default_factory=list)
    ci_status: NonEmptyStr | None = None
    ci_checks: list[CICheckItem] = Field(default_factory=list)
    summary: str
    needs_human_review_count: int = 0
    semantic_groups: list[dict] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
