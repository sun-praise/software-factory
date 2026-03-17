from __future__ import annotations

import subprocess
from typing import Sequence


GIT_COMMAND_TIMEOUT_SECONDS = 30
GH_COMMAND_TIMEOUT_SECONDS = 30
EXCLUDED_COMMIT_PATHS = (".software_factory_bootstrap_state.json",)


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
        return {
            "success": False,
            "commit_sha": None,
            "error": "no_changes",
            "error_stage": "git_diff",
            "remote": remote,
            "branch": branch,
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

    target_branch = branch
    if target_branch is None:
        branch_result = _run_git(repo_dir, ["rev-parse", "--abbrev-ref", "HEAD"])
        if branch_result.returncode != 0:
            return {
                "success": False,
                "commit_sha": commit_sha,
                "error": _pick_message(branch_result),
                "error_stage": "git_branch",
                "remote": remote,
                "branch": None,
                "pushed_ref": None,
            }
        target_branch = branch_result.stdout.strip()
        if not target_branch or target_branch == "HEAD":
            return {
                "success": False,
                "commit_sha": commit_sha,
                "error": "detached_head",
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
