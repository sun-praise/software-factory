from __future__ import annotations

import base64
import dataclasses
import hashlib
import hmac
import json
import logging
import os
from json import JSONDecodeError
from typing import Any, Mapping
from urllib.parse import quote_plus, unquote_plus, urlparse

import httpx

from app.providers.github import (
    _coerce_positive_int,
    _format_manual_issue_context,
    _parse_fragment_numeric_id,
    _parse_positive_int_from_text,
    _parse_repo,
    _safe_text,
)
from app.services.agent_prompt import CHANGED_FILE_PATHS_LIMIT
from app.services.github_events import extract_review_event
from app.services.github_signature import (
    SignatureFailureReason,
    SignatureStatus,
    SignatureVerificationResult,
)
from app.services.git_ops import _resolve_default_base_branch
from app.services.normalizer import normalize_review_events
from app.services.task_source import TEXT_SOURCE_KIND, build_manual_text_task_number


logger = logging.getLogger(__name__)
webhook_logger = logging.getLogger("webhook_debug")
_GITEE_WEB_BASE_URL = "https://gitee.com"
_GITEE_API_BASE_URL = "https://gitee.com/api/v5"
_GITEE_SIGNATURE_HEADER = "X-Gitee-Token"
_GITEE_EVENT_HEADER = "X-Gitee-Event"
_GITEE_TIMESTAMP_HEADER = "X-Gitee-Timestamp"


class GiteeForgeProvider:
    name = "gitee"

    def ensure_pull_request(
        self,
        *,
        repo_dir: str,
        repo: str,
        head_branch: str,
        base_branch: str | None = None,
        title: str,
        body: str,
    ) -> Mapping[str, Any]:
        token = _gitee_token()
        if not token:
            return {
                "success": False,
                "pr_number": None,
                "pr_url": None,
                "error": "GITEE_TOKEN is required to create pull requests.",
                "existing": False,
            }

        resolved_base = base_branch
        if resolved_base is None:
            resolved_base, _ = _resolve_default_base_branch(repo_dir)
        if not resolved_base:
            return {
                "success": False,
                "pr_number": None,
                "pr_url": None,
                "error": "Could not resolve base branch.",
                "existing": False,
            }

        existing = self._find_existing_pull_request(repo=repo, head_branch=head_branch)
        if existing is not None:
            return existing

        try:
            response = httpx.post(
                f"{_GITEE_API_BASE_URL}/repos/{repo}/pulls",
                headers=_gitee_headers(token),
                timeout=10.0,
                json={
                    "head": head_branch,
                    "base": resolved_base,
                    "title": title,
                    "body": body,
                    "access_token": token,
                },
            )
        except httpx.RequestError as exc:
            return {
                "success": False,
                "pr_number": None,
                "pr_url": None,
                "error": str(exc),
                "existing": False,
            }

        payload = _response_json_dict(response)
        if response.status_code >= 400 or payload is None:
            return {
                "success": False,
                "pr_number": None,
                "pr_url": None,
                "error": _response_error_message(response, payload),
                "existing": False,
            }

        pr_number = _coerce_positive_int(payload.get("number"))
        pr_url = _safe_text(payload.get("html_url"))
        return {
            "success": pr_number is not None,
            "pr_number": pr_number,
            "pr_url": pr_url,
            "error": None if pr_number is not None else "missing_pull_request_number",
            "existing": False,
        }

    def _find_existing_pull_request(
        self, *, repo: str, head_branch: str
    ) -> Mapping[str, Any] | None:
        token = _gitee_token()
        if not token:
            return None
        try:
            response = httpx.get(
                f"{_GITEE_API_BASE_URL}/repos/{repo}/pulls",
                headers=_gitee_headers(token),
                timeout=10.0,
                params={"state": "open", "head": head_branch, "per_page": 100},
            )
        except httpx.RequestError:
            return None

        payload = _response_json_list(response)
        if response.status_code >= 400 or payload is None:
            return None
        for item in payload:
            head = item.get("head")
            head_ref = (
                _safe_text(head.get("ref")) if isinstance(head, Mapping) else None
            )
            if head_ref != head_branch:
                continue
            pr_number = _coerce_positive_int(item.get("number"))
            pr_url = _safe_text(item.get("html_url"))
            return {
                "success": True,
                "pr_number": pr_number,
                "pr_url": pr_url,
                "error": None,
                "existing": True,
            }
        return None

    def post_pull_request_comment(
        self,
        *,
        repo_dir: str,
        repo: str,
        pr_number: int,
        body: str,
    ) -> tuple[bool, str]:
        del repo_dir
        token = _gitee_token()
        if not token:
            return False, "GITEE_TOKEN is required to post pull request comments."
        try:
            response = httpx.post(
                f"{_GITEE_API_BASE_URL}/repos/{repo}/pulls/{pr_number}/comments",
                headers=_gitee_headers(token),
                timeout=10.0,
                json={"body": body, "access_token": token},
            )
        except httpx.RequestError as exc:
            return False, str(exc)
        payload = _response_json_dict(response)
        if response.status_code >= 400:
            return False, _response_error_message(response, payload)
        return True, _safe_text((payload or {}).get("html_url")) or "comment_posted"

    def get_pull_request_metadata(
        self,
        *,
        repo_dir: str,
        repo: str,
        pr_number: int,
    ) -> Mapping[str, Any] | None:
        del repo_dir
        if pr_number <= 0:
            return None
        token = _gitee_token()
        try:
            response = httpx.get(
                f"{_GITEE_API_BASE_URL}/repos/{repo}/pulls/{pr_number}",
                headers=_gitee_headers(token),
                timeout=10.0,
            )
        except httpx.RequestError:
            return None
        payload = _response_json_dict(response)
        if response.status_code >= 400 or payload is None:
            return None

        base = payload.get("base")
        head = payload.get("head")
        return {
            "title": _safe_text(payload.get("title")),
            "body": _safe_text(payload.get("body")),
            "base_ref": _safe_text(base.get("ref"))
            if isinstance(base, Mapping)
            else None,
            "head_ref": _safe_text(head.get("ref"))
            if isinstance(head, Mapping)
            else None,
            "head_sha": _safe_text(head.get("sha"))
            if isinstance(head, Mapping)
            else None,
            "changed_files": payload.get("changed_files"),
            "additions": payload.get("additions"),
            "deletions": payload.get("deletions"),
            "merge_state_status": _safe_text(payload.get("state")),
            "can_be_rebased": None,
            "mergeable": payload.get("mergeable"),
            "is_merge_conflict": False,
            "is_behind": False,
            "is_blocked": False,
            "changed_file_paths": self.collect_changed_file_paths(
                repo_dir=repo_dir,
                repo=repo,
                pr_number=pr_number,
            ),
        }

    def collect_changed_file_paths(
        self,
        *,
        repo_dir: str,
        repo: str,
        pr_number: int,
    ) -> list[str]:
        del repo_dir
        token = _gitee_token()
        try:
            response = httpx.get(
                f"{_GITEE_API_BASE_URL}/repos/{repo}/pulls/{pr_number}/files",
                headers=_gitee_headers(token),
                timeout=10.0,
            )
        except httpx.RequestError:
            return []
        payload = _response_json_list(response)
        if response.status_code >= 400 or payload is None:
            return []
        paths = [_safe_text(item.get("filename")) for item in payload]
        return [path for path in paths if path][:CHANGED_FILE_PATHS_LIMIT]


class GiteeTaskSourceProvider:
    name = "gitee"

    def parse_task_submission(
        self, *, submission: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        source_url = _safe_text(submission.get("url"))
        if source_url:
            return self._parse_issue_url(source_url)

        normalized_repo, owner, repo_name = _parse_repo(
            _safe_text(submission.get("repo")) or ""
        )
        task_text = _safe_text(submission.get("text"))
        if not task_text:
            raise ValueError("Task text is required for non-GitHub submissions.")
        task_title = _safe_text(submission.get("title")) or None
        return {
            "repo": normalized_repo,
            "owner": owner,
            "repo_name": repo_name,
            "pr_number": build_manual_text_task_number(
                repo=normalized_repo,
                text=task_text,
                title=task_title,
            ),
            "resolved_pr_number": None,
            "issue_number": None,
            "source_ref": "",
            "source_fragment": "",
            "source_kind": TEXT_SOURCE_KIND,
            "task_title": task_title,
            "task_text": task_text,
        }

    def fetch_pull_request_feedback_review(
        self,
        *,
        repo: str,
        pr_number: int,
    ) -> Mapping[str, Any]:
        pr_comments = self._gitee_get_list(
            f"{_GITEE_API_BASE_URL}/repos/{repo}/pulls/{pr_number}/comments?per_page=100",
            not_found_message="Pull request comments not found or unavailable.",
        )
        issue_comments = self._gitee_get_list(
            f"{_GITEE_API_BASE_URL}/repos/{repo}/issues/{pr_number}/comments?per_page=100",
            not_found_message="Pull request issue comments not found or unavailable.",
        )

        source_ref = f"{_GITEE_WEB_BASE_URL}/{repo}/pulls/{pr_number}"
        events: list[dict[str, Any]] = []
        events.extend(
            {
                "event_type": "pull_request_review_comment",
                "payload": {"comment": comment},
            }
            for comment in pr_comments
        )
        events.extend(
            {
                "event_type": "issue_comment",
                "payload": {
                    "issue": {"pull_request": {"url": source_ref}},
                    "comment": comment,
                },
            }
            for comment in issue_comments
        )

        normalized = normalize_review_events(
            repo=repo,
            pr_number=pr_number,
            events=events,
            head_sha=None,
        )
        normalized["project_type"] = "python"
        normalized["source_kind"] = "pull"
        normalized["resolved_pr_number"] = pr_number
        normalized["manual_issue_source_url"] = source_ref
        normalized["issue_number"] = None
        return normalized

    def resolve_pull_request_number_from_issue(
        self,
        *,
        repo: str,
        issue_number: int,
    ) -> int | None:
        payload = self._gitee_get_json(
            f"{_GITEE_API_BASE_URL}/repos/{repo}/issues/{issue_number}",
            not_found_message="Issue not found or unavailable.",
        )
        pull_request_info = payload.get("pull_request")
        if not isinstance(pull_request_info, Mapping):
            return None
        pr_url = _safe_text(
            pull_request_info.get("html_url") or pull_request_info.get("url")
        )
        if not pr_url:
            return None
        pull_url_parts = [part for part in pr_url.split("/") if part]
        try:
            return int(pull_url_parts[-1])
        except (TypeError, ValueError):
            return None

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
    ) -> Mapping[str, Any] | None:
        fragment = source_fragment.strip().lower()
        try:
            if source_kind == "issue":
                note_id = _parse_fragment_numeric_id(fragment, ("note_",))
                if note_id is not None:
                    return self._fetch_issue_comment_context(
                        repo=repo,
                        comment_id=note_id,
                        source_ref=source_ref,
                    )
                if not fragment:
                    return self._fetch_issue_body_context(
                        repo=repo,
                        issue_number=issue_number or pr_number,
                        source_ref=source_ref,
                    )
                return None

            note_id = _parse_fragment_numeric_id(fragment, ("note_",))
            if note_id is not None:
                return self._fetch_review_comment_context(
                    repo=repo,
                    comment_id=note_id,
                    source_ref=source_ref,
                )
        except ValueError:
            if description_present:
                return None
            raise
        return None

    def _parse_issue_url(self, url: str) -> Mapping[str, Any]:
        normalized_url = url.strip()
        parsed = urlparse(normalized_url)
        if parsed.scheme != "https" or (parsed.hostname or "").lower() != "gitee.com":
            raise ValueError("Only https Gitee links on gitee.com are supported.")

        path_parts = [part for part in parsed.path.split("/") if part]
        if len(path_parts) < 4:
            raise ValueError(
                "Expected a Gitee URL in the form https://gitee.com/<owner>/<repo>/pulls/<number> "
                "or https://gitee.com/<owner>/<repo>/issues/<number>."
            )
        owner, repo_name, section, number_part = path_parts[:4]
        repo = f"{owner}/{repo_name}"
        fragment = parsed.fragment.strip()

        if section in {"pull", "pulls"}:
            pr_number = _parse_positive_int_from_text(
                number_part,
                error_message="PR number in URL must be a positive integer.",
            )
            return {
                "repo": repo,
                "owner": owner,
                "repo_name": repo_name,
                "pr_number": pr_number,
                "resolved_pr_number": pr_number,
                "issue_number": None,
                "source_ref": normalized_url,
                "source_fragment": fragment,
                "source_kind": "pull",
                "task_title": None,
                "task_text": None,
            }

        if section != "issues":
            raise ValueError(
                "Only pull request or issue links are supported. Example: "
                "https://gitee.com/<owner>/<repo>/pulls/<number> or "
                "https://gitee.com/<owner>/<repo>/issues/<number>."
            )

        issue_number = _parse_positive_int_from_text(
            number_part,
            error_message="Issue number in URL must be a positive integer.",
        )
        return {
            "repo": repo,
            "owner": owner,
            "repo_name": repo_name,
            "pr_number": issue_number,
            "resolved_pr_number": None,
            "issue_number": issue_number,
            "source_ref": normalized_url,
            "source_fragment": fragment,
            "source_kind": "issue",
            "task_title": None,
            "task_text": None,
        }

    def _fetch_issue_body_context(
        self,
        *,
        repo: str,
        issue_number: int,
        source_ref: str,
    ) -> Mapping[str, Any]:
        payload = self._gitee_get_json(
            f"{_GITEE_API_BASE_URL}/repos/{repo}/issues/{issue_number}",
            not_found_message="Issue not found or unavailable.",
        )
        title = _safe_text(payload.get("title")) or ""
        body = _safe_text(payload.get("body")) or ""
        if not title and not body:
            raise ValueError(
                "Gitee issue has no body text. Add a description to the manual issue."
            )
        context_body = body or title
        return {
            "text": _format_manual_issue_context(
                label="Gitee issue context",
                title=title,
                body=context_body,
            ),
            "path": None,
            "line": None,
            "source_url": _safe_text(payload.get("html_url")) or source_ref,
        }

    def _fetch_issue_comment_context(
        self,
        *,
        repo: str,
        comment_id: int,
        source_ref: str,
    ) -> Mapping[str, Any]:
        payload = self._gitee_get_json(
            f"{_GITEE_API_BASE_URL}/repos/{repo}/issues/comments/{comment_id}",
            not_found_message="Gitee issue comment not found or unavailable.",
        )
        body = _safe_text(payload.get("body")) or ""
        if not body:
            raise ValueError(
                "Gitee issue comment is empty. Add a description to the manual issue."
            )
        return {
            "text": _format_manual_issue_context(
                label="Gitee issue comment", body=body
            ),
            "path": None,
            "line": None,
            "source_url": _safe_text(payload.get("html_url")) or source_ref,
        }

    def _fetch_review_comment_context(
        self,
        *,
        repo: str,
        comment_id: int,
        source_ref: str,
    ) -> Mapping[str, Any]:
        payload = self._gitee_get_json(
            f"{_GITEE_API_BASE_URL}/repos/{repo}/pulls/comments/{comment_id}",
            not_found_message="Gitee pull request comment not found or unavailable.",
        )
        body = _safe_text(payload.get("body")) or ""
        if not body:
            raise ValueError(
                "Gitee pull request comment is empty. Add a description to the manual issue."
            )
        path = _safe_text(payload.get("path"))
        line = _coerce_positive_int(payload.get("line")) or _coerce_positive_int(
            payload.get("original_line")
        )
        return {
            "text": _format_manual_issue_context(
                label="Gitee pull request comment",
                body=body,
                path=path,
                line=line,
            ),
            "path": path,
            "line": line,
            "source_url": _safe_text(payload.get("html_url")) or source_ref,
        }

    def _gitee_get_json(self, url: str, *, not_found_message: str) -> dict[str, Any]:
        try:
            response = httpx.get(
                url, headers=_gitee_headers(_gitee_token()), timeout=10.0
            )
        except httpx.RequestError as exc:
            raise ValueError(f"Failed to query Gitee details: {exc}") from exc
        if response.status_code == 404:
            raise ValueError(not_found_message)
        if response.status_code == 403:
            raise ValueError(
                "Gitee API access denied while resolving manual issue details."
            )
        if response.status_code == 401:
            raise ValueError("Unauthorized when querying Gitee manual issue details.")
        if response.status_code >= 400:
            raise ValueError(
                f"Gitee API returned unexpected status: {response.status_code}."
            )
        try:
            payload = response.json()
        except JSONDecodeError as exc:
            raise ValueError("Gitee API returned invalid JSON.") from exc
        if not isinstance(payload, dict):
            raise ValueError("Unexpected response from Gitee API.")
        return payload

    def _gitee_get_list(
        self, url: str, *, not_found_message: str
    ) -> list[dict[str, Any]]:
        try:
            response = httpx.get(
                url, headers=_gitee_headers(_gitee_token()), timeout=10.0
            )
        except httpx.RequestError as exc:
            raise ValueError(f"Failed to query Gitee details: {exc}") from exc
        if response.status_code == 404:
            raise ValueError(not_found_message)
        if response.status_code == 403:
            raise ValueError(
                "Gitee API access denied while resolving manual issue details."
            )
        if response.status_code == 401:
            raise ValueError("Unauthorized when querying Gitee manual issue details.")
        if response.status_code >= 400:
            raise ValueError(
                f"Gitee API returned unexpected status: {response.status_code}."
            )
        try:
            payload = response.json()
        except JSONDecodeError as exc:
            raise ValueError("Gitee API returned invalid JSON.") from exc
        if not isinstance(payload, list):
            raise ValueError("Unexpected response from Gitee API.")
        return [item for item in payload if isinstance(item, dict)]


class GiteeWebhookProvider:
    name = "gitee"

    @property
    def signature_header(self) -> str:
        return _GITEE_SIGNATURE_HEADER

    @property
    def event_header(self) -> str:
        return _GITEE_EVENT_HEADER

    def verify_signature(
        self,
        *,
        body: bytes,
        secret: str,
        signature_header: str | None,
        request_headers: Mapping[str, Any] | None = None,
    ) -> SignatureVerificationResult:
        del body
        if not secret.strip():
            return SignatureVerificationResult(status=SignatureStatus.SKIPPED)
        candidate = _safe_text(signature_header)
        if not candidate:
            return SignatureVerificationResult(
                status=SignatureStatus.FAILED,
                reason=SignatureFailureReason.MISSING_HEADER,
            )
        if hmac.compare_digest(candidate, secret.strip()):
            return SignatureVerificationResult(status=SignatureStatus.VERIFIED)
        timestamp = _safe_text((request_headers or {}).get(_GITEE_TIMESTAMP_HEADER))
        if not timestamp and request_headers is not None:
            timestamp = _safe_text(request_headers.get(_GITEE_TIMESTAMP_HEADER.lower()))
        if not timestamp:
            return SignatureVerificationResult(
                status=SignatureStatus.FAILED,
                reason=SignatureFailureReason.MISSING_HEADER,
            )
        expected = _build_gitee_signature(secret=secret.strip(), timestamp=timestamp)
        normalized_candidate = unquote_plus(candidate)
        if hmac.compare_digest(normalized_candidate, expected):
            return SignatureVerificationResult(status=SignatureStatus.VERIFIED)
        return SignatureVerificationResult(
            status=SignatureStatus.FAILED,
            reason=SignatureFailureReason.SIGNATURE_MISMATCH,
        )

    def extract_review_event(
        self,
        *,
        event_type: str,
        payload: Mapping[str, Any],
    ) -> Any:
        normalized_event = event_type.strip().lower()
        if normalized_event != "note hook":
            return None
        noteable_type = _safe_text(payload.get("noteable_type")) or ""
        if noteable_type.lower() not in {"pullrequest", "mergerequest"}:
            return None
        transformed_payload = _build_gitee_issue_comment_payload(payload)
        if transformed_payload is None:
            return None
        event = extract_review_event("issue_comment", transformed_payload)
        if event is None:
            return None
        return dataclasses.replace(
            event,
            event_key=event.event_key.replace("gh:", "gitee:", 1),
        )

    def extract_event_body(
        self,
        *,
        event_type: str,
        payload: Mapping[str, Any],
    ) -> str | None:
        if event_type.strip().lower() != "note hook":
            return None
        comment = payload.get("comment")
        if not isinstance(comment, Mapping):
            return None
        return _safe_text(comment.get("body"))

    def enrich_event_pull_request_info(
        self,
        *,
        event: Any,
        payload: Mapping[str, Any],
        github_token: str,
    ) -> tuple[Any, Mapping[str, Any]]:
        repo = _safe_text(getattr(event, "repo", None))
        pr_number = _coerce_positive_int(getattr(event, "pr_number", None))
        if not repo or pr_number is None:
            return event, payload
        token = _safe_text(github_token)
        if not token:
            webhook_logger.warning(
                "GITEE_TOKEN not set, cannot fetch PR info for %s#%s",
                repo,
                pr_number,
            )
            return event, payload
        try:
            response = httpx.get(
                f"{_GITEE_API_BASE_URL}/repos/{repo}/pulls/{pr_number}",
                headers=_gitee_headers(token),
                timeout=10.0,
            )
            if response.status_code >= 400:
                return event, payload
            pr_data = response.json()
            if not isinstance(pr_data, dict):
                return event, payload
            head = pr_data.get("head")
            head_sha = (
                _safe_text(head.get("sha")) if isinstance(head, Mapping) else None
            )
            if not head_sha:
                return event, payload
            event = dataclasses.replace(event, head_sha=head_sha)
            enriched_payload = dict(payload)
            enriched_payload["pull_request"] = pr_data
            return event, enriched_payload
        except Exception as exc:
            webhook_logger.warning("Failed to fetch PR info from Gitee API: %s", exc)
            return event, payload


class GiteeGitRemoteProvider:
    name = "gitee"

    def build_clone_url(self, repo: str) -> str:
        return f"{_GITEE_WEB_BASE_URL}/{repo}.git"

    def build_pull_request_url(self, *, repo: str, pr_number: int) -> str:
        return f"{_GITEE_WEB_BASE_URL}/{repo}/pulls/{pr_number}"

    @property
    def api_base_url(self) -> str:
        return _GITEE_API_BASE_URL


def _gitee_token() -> str:
    for key in ("GITEE_TOKEN", "GITEE_ACCESS_TOKEN", "GITHUB_TOKEN"):
        value = os.environ.get(key, "").strip()
        if value:
            return value
    return ""


def _gitee_headers(token: str) -> dict[str, str]:
    headers = {"Accept": "application/json", "User-Agent": "software-factory"}
    if token:
        headers["Authorization"] = f"token {token}"
    return headers


def _response_json_dict(response: httpx.Response) -> dict[str, Any] | None:
    try:
        payload = response.json()
    except JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _response_json_list(response: httpx.Response) -> list[dict[str, Any]] | None:
    try:
        payload = response.json()
    except JSONDecodeError:
        return None
    if not isinstance(payload, list):
        return None
    return [item for item in payload if isinstance(item, dict)]


def _response_error_message(
    response: httpx.Response, payload: Mapping[str, Any] | None
) -> str:
    message = _safe_text((payload or {}).get("message"))
    if message:
        return message
    return f"Gitee API returned status {response.status_code}."


def _build_gitee_issue_comment_payload(
    payload: Mapping[str, Any],
) -> dict[str, Any] | None:
    repository = payload.get("repository")
    comment = payload.get("comment")
    pull_request = payload.get("pull_request")
    if not isinstance(repository, Mapping):
        return None
    if not isinstance(comment, Mapping) or not isinstance(pull_request, Mapping):
        return None
    repo = _safe_text(
        repository.get("path_with_namespace") or repository.get("full_name")
    )
    pr_number = _coerce_positive_int(pull_request.get("number"))
    if not repo or pr_number is None:
        return None
    return {
        "repository": {"full_name": repo},
        "issue": {
            "number": pr_number,
            "pull_request": {
                "url": _safe_text(pull_request.get("html_url"))
                or f"{_GITEE_WEB_BASE_URL}/{repo}/pulls/{pr_number}"
            },
        },
        "comment": comment,
        "pull_request": pull_request,
        "sender": payload.get("sender"),
    }


def _build_gitee_signature(*, secret: str, timestamp: str) -> str:
    string_to_sign = f"{timestamp}\n{secret}".encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), string_to_sign, hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def build_signed_gitee_token(*, secret: str, timestamp: str) -> str:
    return quote_plus(_build_gitee_signature(secret=secret, timestamp=timestamp))
