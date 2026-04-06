from __future__ import annotations

import json
import logging
import subprocess
from typing import Any, Mapping

from app.providers.types import DEFAULT_PROVIDER_NAME
from app.services.agent_prompt import CHANGED_FILE_PATHS_LIMIT
from app.services.git_ops import (
    GH_COMMAND_TIMEOUT_SECONDS,
    ensure_pull_request,
    post_pr_comment,
)


logger = logging.getLogger(__name__)
_UNKNOWN_CAN_BE_REBASED_FIELD = 'Unknown JSON field: "canBeRebased"'


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


def _safe_text(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None
