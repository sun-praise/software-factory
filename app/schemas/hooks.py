from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class BaseHookEvent(BaseModel):
    event: str
    session_id: str
    repo: str
    branch: str
    cwd: str
    timestamp: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)
    payload: dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(extra="forbid")


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
