from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


NonEmptyStr = Annotated[str, Field(min_length=1)]


class BaseHookEvent(BaseModel):
    session_id: NonEmptyStr
    repo: NonEmptyStr
    branch: NonEmptyStr
    cwd: NonEmptyStr
    timestamp: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)
    payload: dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    @field_validator("timestamp")
    @classmethod
    def ensure_timestamp_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must include timezone")
        return value


class UserPromptSubmitEvent(BaseHookEvent):
    event: Literal["UserPromptSubmit"]


class PostToolUseEvent(BaseHookEvent):
    event: Literal["PostToolUse"]


class PostToolUseFailureEvent(BaseHookEvent):
    event: Literal["PostToolUseFailure"]


HookEvent = Annotated[
    UserPromptSubmitEvent | PostToolUseEvent | PostToolUseFailureEvent,
    Field(discriminator="event"),
]
