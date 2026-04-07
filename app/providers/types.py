from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol, runtime_checkable


DEFAULT_PROVIDER_NAME = "github"


@dataclass(frozen=True, slots=True)
class PullRequestUpsertResult:
    success: bool
    pr_number: int | None
    pr_url: str | None
    error: str | None = None
    existing: bool = False


@dataclass(frozen=True, slots=True)
class PullRequestMetadata:
    title: str | None = None
    body: str | None = None
    base_ref_name: str | None = None
    head_ref_name: str | None = None
    head_ref_oid: str | None = None
    merge_state_status: str | None = None
    can_be_rebased: bool | None = None


@runtime_checkable
class ForgeProvider(Protocol):
    name: str

    def ensure_pull_request(
        self,
        *,
        repo_dir: str,
        repo: str,
        head_branch: str,
        base_branch: str | None = None,
        title: str,
        body: str,
    ) -> PullRequestUpsertResult | Mapping[str, Any]: ...

    def post_pull_request_comment(
        self,
        *,
        repo_dir: str,
        repo: str,
        pr_number: int,
        body: str,
    ) -> tuple[bool, str]: ...

    def get_pull_request_metadata(
        self,
        *,
        repo_dir: str,
        repo: str,
        pr_number: int,
    ) -> PullRequestMetadata | Mapping[str, Any] | None: ...

    def collect_changed_file_paths(
        self,
        *,
        repo_dir: str,
        repo: str,
        pr_number: int,
    ) -> list[str]: ...


@runtime_checkable
class TaskSourceProvider(Protocol):
    name: str

    def parse_task_submission(
        self, *, submission: Mapping[str, Any]
    ) -> Mapping[str, Any]: ...

    def fetch_pull_request_feedback_review(
        self,
        *,
        repo: str,
        pr_number: int,
    ) -> Mapping[str, Any]: ...

    def resolve_pull_request_number_from_issue(
        self,
        *,
        repo: str,
        issue_number: int,
    ) -> int | None: ...

    def resolve_manual_issue_context(
        self,
        *,
        repo: str,
        pr_number: int,
        issue_number: int | None,
        source_kind: str,
        source_ref: str,
        source_fragment: str,
        description_present: bool,
    ) -> Mapping[str, Any] | None: ...


@runtime_checkable
class WebhookProvider(Protocol):
    name: str

    @property
    def signature_header(self) -> str: ...

    def verify_signature(
        self,
        *,
        body: bytes,
        secret: str,
        signature_header: str | None,
    ) -> Any: ...

    def extract_review_event(
        self,
        *,
        event_type: str,
        payload: Mapping[str, Any],
    ) -> Any: ...

    def extract_event_body(
        self,
        *,
        event_type: str,
        payload: Mapping[str, Any],
    ) -> str | None: ...


@runtime_checkable
class GitRemoteProvider(Protocol):
    name: str

    def build_clone_url(self, repo: str) -> str: ...

    def build_pull_request_url(self, *, repo: str, pr_number: int) -> str: ...

    @property
    def api_base_url(self) -> str: ...
