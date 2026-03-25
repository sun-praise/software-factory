from __future__ import annotations

import json
import re
import subprocess
from typing import Sequence


GIT_COMMAND_TIMEOUT_SECONDS = 30
GH_COMMAND_TIMEOUT_SECONDS = 30
EXCLUDED_COMMIT_PATHS = (".software_factory_bootstrap_state.json",)
_PULL_REQUEST_URL_PATTERN = re.compile(r"/pull/(\d+)(?:\D|$)")
_PULL_REQUEST_URL_CAPTURE_PATTERN = re.compile(
    r"https?://[^\s\"')>]+/pull/\d+(?:[^\s\"')>]*)?"
)


def _run_git(repo_dir: str, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=repo_dir,
            check=False,
            capture_output=True,
            text=True,
            timeout=GIT_COMMAND_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raw_stderr = exc.stderr or ""
        if isinstance(raw_stderr, bytes):
            stderr = raw_stderr.decode("utf-8", errors="replace").strip()
        else:
            stderr = str(raw_stderr).strip()
        message = (
            stderr or f"git command timed out after {GIT_COMMAND_TIMEOUT_SECONDS}s"
        )
        return subprocess.CompletedProcess(
            args=["git", *args], returncode=124, stdout="", stderr=message
        )


def _pick_message(result: subprocess.CompletedProcess[str]) -> str:
    message = result.stderr.strip() or result.stdout.strip()
    if message:
        return message
    return f"git exited with code {result.returncode}"


def _run_gh(repo_dir: str, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["gh", *args],
            cwd=repo_dir,
            check=False,
            capture_output=True,
            text=True,
            timeout=GH_COMMAND_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raw_stderr = exc.stderr or ""
        if isinstance(raw_stderr, bytes):
            stderr = raw_stderr.decode("utf-8", errors="replace").strip()
        else:
            stderr = str(raw_stderr).strip()
        message = stderr or f"gh command timed out after {GH_COMMAND_TIMEOUT_SECONDS}s"
        return subprocess.CompletedProcess(
            args=["gh", *args], returncode=124, stdout="", stderr=message
        )


def _resolve_target_branch(
    repo_dir: str, branch: str | None
) -> tuple[str | None, str | None]:
    if branch is not None:
        return branch, None
    branch_result = _run_git(repo_dir, ["rev-parse", "--abbrev-ref", "HEAD"])
    if branch_result.returncode != 0:
        return None, _pick_message(branch_result)
    target_branch = branch_result.stdout.strip()
    if not target_branch or target_branch == "HEAD":
        return None, "detached_head"
    return target_branch, None


def _resolve_default_base_branch(
    repo_dir: str, remote: str = "origin"
) -> tuple[str | None, str | None]:
    result = _run_git(
        repo_dir, ["symbolic-ref", "--short", f"refs/remotes/{remote}/HEAD"]
    )
    if result.returncode != 0:
        return None, _pick_message(result)
    ref = result.stdout.strip()
    prefix = f"{remote}/"
    if not ref.startswith(prefix):
        return None, f"unexpected_remote_head_ref: {ref or 'empty'}"
    base_branch = ref[len(prefix) :].strip()
    if not base_branch:
        return None, "empty_default_base_branch"
    return base_branch, None


def _parse_pull_request_number(pr_url: str) -> int | None:
    match = _PULL_REQUEST_URL_PATTERN.search(pr_url)
    if match is None:
        return None
    try:
        number = int(match.group(1))
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _extract_pull_request_url(output: str) -> str | None:
    matches = _PULL_REQUEST_URL_CAPTURE_PATTERN.findall(output or "")
    for candidate in reversed(matches):
        if _parse_pull_request_number(candidate) is not None:
            return candidate
    return None


def _find_existing_pull_request(
    repo_dir: str,
    repo: str,
    head_branch: str,
) -> dict[str, object]:
    result = _run_gh(
        repo_dir,
        [
            "pr",
            "list",
            "--repo",
            repo,
            "--head",
            head_branch,
            "--state",
            "all",
            "--json",
            "number,url",
            "--limit",
            "1",
        ],
    )
    if result.returncode != 0:
        return {
            "success": False,
            "pr_number": None,
            "pr_url": None,
            "error": _pick_message(result),
            "existing": False,
        }
    try:
        payload = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return {
            "success": False,
            "pr_number": None,
            "pr_url": None,
            "error": "invalid_pr_list_payload",
            "existing": False,
        }
    if not isinstance(payload, list) or not payload:
        return {
            "success": True,
            "pr_number": None,
            "pr_url": None,
            "error": None,
            "existing": False,
        }
    first_item = payload[0]
    if not isinstance(first_item, dict):
        return {
            "success": False,
            "pr_number": None,
            "pr_url": None,
            "error": "invalid_pr_list_item",
            "existing": False,
        }
    pr_url = str(first_item.get("url") or "").strip()
    pr_number = first_item.get("number")
    if isinstance(pr_number, bool):
        pr_number = None
    if pr_number is None and pr_url:
        pr_number = _parse_pull_request_number(pr_url)
    elif pr_number is not None:
        try:
            parsed_number = int(str(pr_number).strip())
        except (TypeError, ValueError):
            parsed_number = None
        pr_number = parsed_number if parsed_number and parsed_number > 0 else None
    return {
        "success": True,
        "pr_number": pr_number,
        "pr_url": pr_url or None,
        "error": None,
        "existing": True,
    }


def ensure_head_sha(repo_dir: str, expected_sha: str) -> bool:
    result = _run_git(repo_dir, ["rev-parse", "HEAD"])
    if result.returncode != 0:
        return False
    return result.stdout.strip() == expected_sha.strip()


def checkout_branch(repo_dir: str, branch: str) -> tuple[bool, str]:
    result = _run_git(repo_dir, ["checkout", branch])
    success = result.returncode == 0
    if success:
        message = result.stdout.strip() or f"checked out {branch}"
    else:
        message = _pick_message(result)
    return success, message


def commit_and_push(
    repo_dir: str,
    message: str,
    remote: str = "origin",
    branch: str | None = None,
) -> dict:
    add_result = _run_git(repo_dir, ["add", "-A"])
    if add_result.returncode != 0:
        return {
            "success": False,
            "commit_sha": None,
            "error": _pick_message(add_result),
            "error_stage": "git_add",
            "remote": remote,
            "branch": branch,
            "pushed_ref": None,
        }

    for excluded_path in EXCLUDED_COMMIT_PATHS:
        staged_path_result = _run_git(
            repo_dir,
            ["diff", "--cached", "--name-only", "--", excluded_path],
        )
        if staged_path_result.returncode != 0:
            return {
                "success": False,
                "commit_sha": None,
                "error": _pick_message(staged_path_result),
                "error_stage": "git_exclude",
                "remote": remote,
                "branch": branch,
                "pushed_ref": None,
            }
        if not staged_path_result.stdout.strip():
            continue
        unstage_result = _run_git(
            repo_dir,
            ["reset", "--quiet", "HEAD", "--", excluded_path],
        )
        if unstage_result.returncode != 0:
            return {
                "success": False,
                "commit_sha": None,
                "error": _pick_message(unstage_result),
                "error_stage": "git_exclude",
                "remote": remote,
                "branch": branch,
                "pushed_ref": None,
            }

    diff_result = _run_git(repo_dir, ["diff", "--cached", "--quiet"])
    if diff_result.returncode == 0:
        target_branch, branch_error = _resolve_target_branch(repo_dir, branch)
        if target_branch and not branch_error:
            ahead_result = _run_git(
                repo_dir,
                [
                    "rev-list",
                    "--left-right",
                    "--count",
                    f"{remote}/{target_branch}...HEAD",
                ],
            )
            if ahead_result.returncode == 0:
                counts = ahead_result.stdout.strip().split()
                if len(counts) == 2:
                    try:
                        ahead_count = int(counts[1])
                    except ValueError:
                        ahead_count = 0
                    if ahead_count > 0:
                        sha_result = _run_git(repo_dir, ["rev-parse", "HEAD"])
                        if sha_result.returncode != 0:
                            return {
                                "success": False,
                                "commit_sha": None,
                                "error": _pick_message(sha_result),
                                "error_stage": "git_rev_parse",
                                "remote": remote,
                                "branch": target_branch,
                                "pushed_ref": None,
                            }
                        commit_sha = sha_result.stdout.strip()
                        if not commit_sha:
                            return {
                                "success": False,
                                "commit_sha": None,
                                "error": "empty_commit_sha",
                                "error_stage": "git_rev_parse",
                                "remote": remote,
                                "branch": target_branch,
                                "pushed_ref": None,
                            }
                        push_result = _run_git(
                            repo_dir, ["push", remote, target_branch]
                        )
                        if push_result.returncode != 0:
                            return {
                                "success": False,
                                "commit_sha": commit_sha,
                                "error": _pick_message(push_result),
                                "error_stage": "git_push",
                                "remote": remote,
                                "branch": target_branch,
                                "pushed_ref": f"{remote}/{target_branch}",
                            }
                        return {
                            "success": True,
                            "commit_sha": commit_sha,
                            "error": None,
                            "error_stage": None,
                            "remote": remote,
                            "branch": target_branch,
                            "pushed_ref": f"{remote}/{target_branch}",
                        }
        return {
            "success": False,
            "commit_sha": None,
            "error": "no_changes",
            "error_stage": "git_diff",
            "remote": remote,
            "branch": target_branch if target_branch else branch,
            "pushed_ref": None,
        }
    if diff_result.returncode != 1:
        return {
            "success": False,
            "commit_sha": None,
            "error": _pick_message(diff_result),
            "error_stage": "git_diff",
            "remote": remote,
            "branch": branch,
            "pushed_ref": None,
        }

    commit_result = _run_git(repo_dir, ["commit", "-m", message])
    if commit_result.returncode != 0:
        return {
            "success": False,
            "commit_sha": None,
            "error": _pick_message(commit_result),
            "error_stage": "git_commit",
            "remote": remote,
            "branch": branch,
            "pushed_ref": None,
        }

    sha_result = _run_git(repo_dir, ["rev-parse", "HEAD"])
    if sha_result.returncode != 0:
        return {
            "success": False,
            "commit_sha": None,
            "error": _pick_message(sha_result),
            "error_stage": "git_rev_parse",
            "remote": remote,
            "branch": branch,
            "pushed_ref": None,
        }

    commit_sha = sha_result.stdout.strip()
    if not commit_sha:
        return {
            "success": False,
            "commit_sha": None,
            "error": "empty_commit_sha",
            "error_stage": "git_rev_parse",
            "remote": remote,
            "branch": branch,
            "pushed_ref": None,
        }

    target_branch, branch_error = _resolve_target_branch(repo_dir, branch)
    if branch_error or not target_branch:
        return {
            "success": False,
            "commit_sha": commit_sha,
            "error": branch_error or "detached_head",
            "error_stage": "git_branch",
            "remote": remote,
            "branch": target_branch or None,
            "pushed_ref": None,
        }

    push_result = _run_git(repo_dir, ["push", remote, target_branch])
    if push_result.returncode != 0:
        return {
            "success": False,
            "commit_sha": commit_sha,
            "error": _pick_message(push_result),
            "error_stage": "git_push",
            "remote": remote,
            "branch": target_branch,
            "pushed_ref": f"{remote}/{target_branch}",
        }

    return {
        "success": True,
        "commit_sha": commit_sha,
        "error": None,
        "error_stage": None,
        "remote": remote,
        "branch": target_branch,
        "pushed_ref": f"{remote}/{target_branch}",
    }


def rebase_onto_base(
    repo_dir: str, base_ref: str, remote: str = "origin"
) -> tuple[bool, str, bool]:
    fetch_result = _run_git(repo_dir, ["fetch", remote, base_ref])
    fetch_failed = fetch_result.returncode != 0
    if fetch_failed:
        remote_ref = base_ref
    else:
        remote_ref = f"{remote}/{base_ref}"

    rebase_result = _run_git(repo_dir, ["rebase", remote_ref])
    if rebase_result.returncode == 0:
        message = rebase_result.stdout.strip() or f"rebased onto {remote_ref}"
        if fetch_failed:
            message = f"rebase succeeded but fetch had failed (rebased to local {base_ref}): {message}"
        return True, message, False

    abort_result = _run_git(repo_dir, ["rebase", "--abort"])
    raw_message = rebase_result.stderr.strip() or rebase_result.stdout.strip() or ""
    is_conflict = _is_rebase_conflict(rebase_result)
    if is_conflict:
        if abort_result.returncode != 0:
            raw_message = (
                f"{raw_message}; abort also failed: {_pick_message(abort_result)}"
            )
        return False, f"rebase_conflict: {raw_message}", True

    if fetch_failed:
        return (
            False,
            f"rebase_fetch_failed: unable to fetch {remote}/{base_ref} and rebase failed - {raw_message}",
            False,
        )

    return False, f"rebase_failed: {raw_message}", False


def _is_rebase_conflict(result: subprocess.CompletedProcess[str]) -> bool:
    combined = f"{result.stderr} {result.stdout}".lower()
    conflict_indicators = (
        "conflict",
        "could not apply",
        "unresolved conflicts",
        "merge conflict",
        "patch failed",
    )
    return any(indicator in combined for indicator in conflict_indicators)


def post_pr_comment(
    repo_dir: str,
    repo: str,
    pr_number: int,
    body: str,
) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            [
                "gh",
                "pr",
                "comment",
                str(pr_number),
                "--repo",
                repo,
                "--body",
                body,
            ],
            cwd=repo_dir,
            check=False,
            capture_output=True,
            text=True,
            timeout=GH_COMMAND_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return False, f"gh pr comment timed out after {GH_COMMAND_TIMEOUT_SECONDS}s"
    if result.returncode == 0:
        output = result.stdout.strip() or "comment_posted"
        return True, output
    return False, _pick_message(result)


def ensure_pull_request(
    repo_dir: str,
    repo: str,
    head_branch: str,
    *,
    title: str,
    body: str,
    base_branch: str | None = None,
    remote: str = "origin",
) -> dict[str, object]:
    normalized_head_branch = head_branch.strip()
    if not normalized_head_branch:
        return {
            "success": False,
            "pr_number": None,
            "pr_url": None,
            "base_branch": None,
            "error": "missing_head_branch",
            "existing": False,
        }

    existing_result = _find_existing_pull_request(
        repo_dir, repo, normalized_head_branch
    )
    if not existing_result.get("success"):
        return {
            **existing_result,
            "base_branch": base_branch,
        }
    if existing_result.get("existing") and existing_result.get("pr_number"):
        return {
            **existing_result,
            "base_branch": base_branch,
        }

    resolved_base_branch = (base_branch or "").strip()
    if not resolved_base_branch:
        resolved_base_branch, base_error = _resolve_default_base_branch(
            repo_dir, remote=remote
        )
        if base_error or not resolved_base_branch:
            return {
                "success": False,
                "pr_number": None,
                "pr_url": None,
                "base_branch": None,
                "error": base_error or "missing_base_branch",
                "existing": False,
            }

    result = _run_gh(
        repo_dir,
        [
            "pr",
            "create",
            "--repo",
            repo,
            "--head",
            normalized_head_branch,
            "--base",
            resolved_base_branch,
            "--title",
            title,
            "--body",
            body,
        ],
    )
    if result.returncode == 0:
        pr_url = _extract_pull_request_url(result.stdout) or ""
        pr_number = _parse_pull_request_number(pr_url)
        if pr_number is None or not pr_url:
            existing_after_create = _find_existing_pull_request(
                repo_dir, repo, normalized_head_branch
            )
            if existing_after_create.get("success") and existing_after_create.get(
                "pr_number"
            ):
                return {
                    **existing_after_create,
                    "base_branch": resolved_base_branch,
                }
            return {
                "success": False,
                "pr_number": None,
                "pr_url": None,
                "base_branch": resolved_base_branch,
                "error": "missing_created_pr_metadata",
                "existing": False,
            }
        return {
            "success": True,
            "pr_number": pr_number,
            "pr_url": pr_url,
            "base_branch": resolved_base_branch,
            "error": None,
            "existing": False,
        }

    existing_after_failure = _find_existing_pull_request(
        repo_dir, repo, normalized_head_branch
    )
    if existing_after_failure.get("success") and existing_after_failure.get(
        "pr_number"
    ):
        return {
            **existing_after_failure,
            "base_branch": resolved_base_branch,
        }
    return {
        "success": False,
        "pr_number": None,
        "pr_url": None,
        "base_branch": resolved_base_branch,
        "error": _pick_message(result),
        "existing": False,
    }
