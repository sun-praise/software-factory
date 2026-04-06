from __future__ import annotations

import json
import os
import logging
import subprocess
from json import JSONDecodeError
from typing import Any, Mapping
from urllib.parse import urlparse

import httpx

from app.providers.types import DEFAULT_PROVIDER_NAME
from app.services.agent_prompt import CHANGED_FILE_PATHS_LIMIT
from app.services.github_events import extract_event_body, extract_review_event
from app.services.github_signature import (
    GITHUB_SIGNATURE_HEADER,
    verify_github_signature,
)
from app.services.git_ops import (
    GH_COMMAND_TIMEOUT_SECONDS,
    ensure_pull_request,
    post_pr_comment,
)
from app.services.normalizer import normalize_review_events
from app.services.task_source import TEXT_SOURCE_KIND, build_manual_text_task_number


logger = logging.getLogger(__name__)
_UNKNOWN_CAN_BE_REBASED_FIELD = 'Unknown JSON field: "canBeRebased"'
_GITHUB_API_BASE_URL = "https://api.github.com"


class GitHubForgeProvider:
    name = DEFAULT_PROVIDER_NAME

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
        return ensure_pull_request(
            repo_dir,
            repo,
            head_branch,
            title=title,
            body=body,
            base_branch=base_branch,
        )

    def post_pull_request_comment(
        self,
        *,
        repo_dir: str,
        repo: str,
        pr_number: int,
        body: str,
    ) -> tuple[bool, str]:
        return post_pr_comment(repo_dir, repo, pr_number, body)

    def get_pull_request_metadata(
        self,
        *,
        repo_dir: str,
        repo: str,
        pr_number: int,
    ) -> Mapping[str, Any] | None:
        if pr_number <= 0:
            return None

        primary_fields = (
            "title,body,baseRefName,headRefName,headRefOid,changedFiles,"
            "additions,deletions,mergeStateStatus,canBeRebased,mergeable"
        )
        fallback_fields = (
            "title,body,baseRefName,headRefName,headRefOid,changedFiles,"
            "additions,deletions,mergeStateStatus,mergeable"
        )

        result = self._run_gh_pr_view(
            repo_dir=repo_dir,
            repo=repo,
            pr_number=pr_number,
            json_fields=primary_fields,
        )
        if result is None:
            return None

        if result.returncode != 0:
            details = _gh_result_error_details(result)
            if _UNKNOWN_CAN_BE_REBASED_FIELD in details:
                logger.warning(
                    "gh missing canBeRebased field; retrying metadata fetch without it: repo=%s pr=%s",
                    repo,
                    pr_number,
                )
                result = self._run_gh_pr_view(
                    repo_dir=repo_dir,
                    repo=repo,
                    pr_number=pr_number,
                    json_fields=fallback_fields,
                )
                if result is None:
                    return None
                details = (
                    "" if result.returncode == 0 else _gh_result_error_details(result)
                )
            if details:
                logger.warning(
                    "failed to fetch PR metadata via gh: repo=%s pr=%s error=%s",
                    repo,
                    pr_number,
                    details,
                )
                return None

        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            logger.warning(
                "invalid PR metadata payload from gh: repo=%s pr=%s error=%s",
                repo,
                pr_number,
                exc,
            )
            return None
        if not isinstance(payload, Mapping):
            logger.warning(
                "unexpected PR metadata payload type from gh: repo=%s pr=%s payload=%r",
                repo,
                pr_number,
                payload,
            )
            return None

        merge_state_status = _safe_text(payload.get("mergeStateStatus"))
        is_unknown_state = merge_state_status in {"UNKNOWN", "UNSTABLE"}
        if is_unknown_state:
            logger.warning(
                "pr_merge_state_unknown: repo=%s pr=%s merge_state_status=%s",
                repo,
                pr_number,
                merge_state_status,
            )

        return {
            "title": _safe_text(payload.get("title")),
            "body": _safe_text(payload.get("body")),
            "base_ref": _safe_text(payload.get("baseRefName")),
            "head_ref": _safe_text(payload.get("headRefName")),
            "head_sha": _safe_text(payload.get("headRefOid")),
            "changed_files": payload.get("changedFiles"),
            "additions": payload.get("additions"),
            "deletions": payload.get("deletions"),
            "merge_state_status": merge_state_status,
            "can_be_rebased": payload.get("canBeRebased"),
            "mergeable": payload.get("mergeable"),
            "is_merge_conflict": merge_state_status in {"CONFLICTING", "DIRTY"},
            "is_behind": merge_state_status == "BEHIND",
            "is_blocked": merge_state_status == "BLOCKED",
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
        try:
            result = subprocess.run(
                [
                    "gh",
                    "pr",
                    "diff",
                    str(pr_number),
                    "--repo",
                    repo,
                    "--name-only",
                ],
                cwd=repo_dir,
                check=False,
                capture_output=True,
                text=True,
                timeout=GH_COMMAND_TIMEOUT_SECONDS,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return []
        if result.returncode != 0:
            return []
        paths = [
            line.strip() for line in result.stdout.strip().splitlines() if line.strip()
        ]
        if not paths:
            return []
        return paths[:CHANGED_FILE_PATHS_LIMIT]

    def _run_gh_pr_view(
        self,
        *,
        repo_dir: str,
        repo: str,
        pr_number: int,
        json_fields: str,
    ) -> subprocess.CompletedProcess[str] | None:
        try:
            return subprocess.run(
                [
                    "gh",
                    "pr",
                    "view",
                    str(pr_number),
                    "--repo",
                    repo,
                    "--json",
                    json_fields,
                ],
                cwd=repo_dir,
                check=False,
                capture_output=True,
                text=True,
                timeout=GH_COMMAND_TIMEOUT_SECONDS,
            )
        except FileNotFoundError:
            logger.warning(
                "failed to fetch PR metadata via gh: repo=%s pr=%s error=gh not installed",
                repo,
                pr_number,
            )
        except subprocess.TimeoutExpired:
            logger.warning(
                "failed to fetch PR metadata via gh: repo=%s pr=%s error=timeout",
                repo,
                pr_number,
            )
        return None


class GitHubTaskSourceProvider:
    name = DEFAULT_PROVIDER_NAME

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
        review_comments = self._github_get_list(
            f"{_GITHUB_API_BASE_URL}/repos/{repo}/pulls/{pr_number}/comments?per_page=100",
            not_found_message="Pull request review comments not found or unavailable.",
        )
        issue_comments = self._github_get_list(
            f"{_GITHUB_API_BASE_URL}/repos/{repo}/issues/{pr_number}/comments?per_page=100",
            not_found_message="Pull request issue comments not found or unavailable.",
        )
        reviews = self._github_get_list(
            f"{_GITHUB_API_BASE_URL}/repos/{repo}/pulls/{pr_number}/reviews?per_page=100",
            not_found_message="Pull request reviews not found or unavailable.",
        )

        source_ref = f"https://github.com/{repo}/pull/{pr_number}"
        events: list[dict[str, Any]] = []
        events.extend(
            {
                "event_type": "pull_request_review_comment",
                "payload": {"comment": comment},
            }
            for comment in review_comments
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
        events.extend(
            {"event_type": "pull_request_review", "payload": {"review": review}}
            for review in reviews
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
        payload = self._github_get_json(
            f"{_GITHUB_API_BASE_URL}/repos/{repo}/issues/{issue_number}",
            not_found_message="Issue not found or unavailable.",
        )

        pull_request_info = payload.get("pull_request")
        if not isinstance(pull_request_info, dict):
            return None

        pr_url = pull_request_info.get("url", "")
        if not isinstance(pr_url, str):
            return None

        pull_url_parts = [part for part in pr_url.split("/") if part]
        try:
            return int(pull_url_parts[-1])
        except (TypeError, ValueError):
            return None

    def _parse_issue_url(self, url: str) -> Mapping[str, Any]:
        normalized_url = url.strip()
        parsed = urlparse(normalized_url)
        if parsed.scheme != "https" or (parsed.hostname or "").lower() != "github.com":
            raise ValueError("Only https GitHub links on github.com are supported.")

        path_parts = [part for part in parsed.path.split("/") if part]
        if len(path_parts) < 4:
            raise ValueError(
                "Expected a GitHub URL in the form https://github.com/<owner>/<repo>/pull/<number> "
                "or https://github.com/<owner>/<repo>/issues/<number>."
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
                "https://github.com/<owner>/<repo>/pull/<number> or "
                "https://github.com/<owner>/<repo>/issues/<number>."
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

    def _github_headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "software-factory",
        }
        token = self._github_token()
        if token:
            headers["Authorization"] = f"token {token}"
        return headers

    def _github_token(self) -> str:
        for key in (
            "GITHUB_TOKEN",
            "GH_TOKEN",
            "GITHUB_PERSONAL_ACCESS_TOKEN",
            "GITHUB_RELEASE_TOKEN",
        ):
            value = os.environ.get(key, "").strip()
            if value:
                return value
        return ""

    def _github_get_json(self, url: str, *, not_found_message: str) -> dict[str, Any]:
        try:
            response = httpx.get(url, headers=self._github_headers(), timeout=10.0)
        except httpx.RequestError as exc:
            raise ValueError(f"Failed to query GitHub details: {exc}") from exc

        if response.status_code == 404:
            raise ValueError(not_found_message)
        if response.status_code == 403:
            raise ValueError(
                "GitHub API access denied while resolving manual issue details."
            )
        if response.status_code == 401:
            raise ValueError("Unauthorized when querying GitHub manual issue details.")
        if response.status_code >= 400:
            raise ValueError(
                f"GitHub API returned unexpected status: {response.status_code}."
            )

        try:
            payload = response.json()
        except JSONDecodeError as exc:
            raise ValueError("GitHub API returned invalid JSON.") from exc
        if not isinstance(payload, dict):
            raise ValueError("Unexpected response from GitHub API.")
        return payload

    def _github_get_list(
        self, url: str, *, not_found_message: str
    ) -> list[dict[str, Any]]:
        try:
            response = httpx.get(url, headers=self._github_headers(), timeout=10.0)
        except httpx.RequestError as exc:
            raise ValueError(f"Failed to query GitHub details: {exc}") from exc

        if response.status_code == 404:
            raise ValueError(not_found_message)
        if response.status_code == 403:
            raise ValueError(
                "GitHub API access denied while resolving manual issue details."
            )
        if response.status_code == 401:
            raise ValueError("Unauthorized when querying GitHub manual issue details.")
        if response.status_code >= 400:
            raise ValueError(
                f"GitHub API returned unexpected status: {response.status_code}."
            )

        try:
            payload = response.json()
        except JSONDecodeError as exc:
            raise ValueError("GitHub API returned invalid JSON.") from exc
        if not isinstance(payload, list):
            raise ValueError("Unexpected response from GitHub API.")
        return [item for item in payload if isinstance(item, dict)]


class GitHubWebhookProvider:
    name = DEFAULT_PROVIDER_NAME

    @property
    def signature_header(self) -> str:
        return GITHUB_SIGNATURE_HEADER

    def verify_signature(
        self,
        *,
        body: bytes,
        secret: str,
        signature_header: str | None,
    ) -> Any:
        return verify_github_signature(
            body=body,
            secret=secret,
            signature_header=signature_header,
        )

    def extract_review_event(
        self,
        *,
        event_type: str,
        payload: Mapping[str, Any],
    ) -> Any:
        return extract_review_event(event_type=event_type, payload=payload)

    def extract_event_body(
        self,
        *,
        event_type: str,
        payload: Mapping[str, Any],
    ) -> str | None:
        return extract_event_body(event_type=event_type, payload=payload)


class GitHubGitRemoteProvider:
    name = DEFAULT_PROVIDER_NAME

    def build_clone_url(self, repo: str) -> str:
        return f"https://github.com/{repo}.git"

    def build_pull_request_url(self, *, repo: str, pr_number: int) -> str:
        return f"https://github.com/{repo}/pull/{pr_number}"

    @property
    def api_base_url(self) -> str:
        return "https://api.github.com"


def _gh_result_error_details(result: subprocess.CompletedProcess[str]) -> str:
    return result.stderr.strip() or result.stdout.strip() or "unknown gh error"


def _parse_repo(repo: str) -> tuple[str, str, str]:
    normalized_repo = repo.strip()
    repo_parts = [part for part in normalized_repo.split("/") if part]
    if len(repo_parts) != 2:
        raise ValueError("Repository must be in the form <owner>/<repo>.")
    owner, repo_name = repo_parts
    return normalized_repo, owner, repo_name


def _parse_positive_int_from_text(raw_value: str, *, error_message: str) -> int:
    try:
        parsed = int(raw_value)
    except ValueError as exc:
        raise ValueError(error_message) from exc
    if parsed <= 0:
        raise ValueError(error_message)
    return parsed


def _safe_text(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None
