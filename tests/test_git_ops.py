from __future__ import annotations

import subprocess

from app.services import git_ops


def _cp(
    args: list[str],
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=args,
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


def _patch_run(
    monkeypatch,
    scripted: list[tuple[list[str], subprocess.CompletedProcess[str]]],
) -> list[list[str]]:
    calls: list[list[str]] = []
    script_iter = iter(scripted)

    def fake_run(command, **kwargs):
        calls.append(command)
        try:
            expected_command, result = next(script_iter)
        except StopIteration as exc:
            raise AssertionError(f"Unexpected subprocess.run call: {command}") from exc

        assert command == expected_command
        assert kwargs["check"] is False
        assert kwargs["capture_output"] is True
        assert kwargs["text"] is True
        assert kwargs["timeout"] == 30
        return result

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


def test_ensure_head_sha_returns_true_when_match(monkeypatch) -> None:
    _patch_run(
        monkeypatch,
        [
            (
                ["git", "rev-parse", "HEAD"],
                _cp(["git", "rev-parse", "HEAD"], stdout="abc123\n"),
            )
        ],
    )

    assert git_ops.ensure_head_sha("/repo", "abc123") is True


def test_ensure_head_sha_returns_false_when_mismatch(monkeypatch) -> None:
    _patch_run(
        monkeypatch,
        [
            (
                ["git", "rev-parse", "HEAD"],
                _cp(["git", "rev-parse", "HEAD"], stdout="abc123\n"),
            )
        ],
    )

    assert git_ops.ensure_head_sha("/repo", "def456") is False


def test_checkout_branch_success(monkeypatch) -> None:
    _patch_run(
        monkeypatch,
        [
            (
                ["git", "checkout", "feature/m5"],
                _cp(
                    ["git", "checkout", "feature/m5"],
                    stdout="Switched to branch 'feature/m5'\n",
                ),
            )
        ],
    )

    success, message = git_ops.checkout_branch("/repo", "feature/m5")
    assert success is True
    assert "feature/m5" in message


def test_checkout_branch_failure(monkeypatch) -> None:
    _patch_run(
        monkeypatch,
        [
            (
                ["git", "checkout", "missing"],
                _cp(
                    ["git", "checkout", "missing"],
                    returncode=1,
                    stderr="error: pathspec 'missing' did not match\n",
                ),
            )
        ],
    )

    success, message = git_ops.checkout_branch("/repo", "missing")
    assert success is False
    assert "pathspec" in message


def test_commit_and_push_returns_no_changes(monkeypatch) -> None:
    calls = _patch_run(
        monkeypatch,
        [
            (["git", "add", "-A"], _cp(["git", "add", "-A"])),
            (
                [
                    "git",
                    "diff",
                    "--cached",
                    "--name-only",
                    "--",
                    ".software_factory_bootstrap_state.json",
                ],
                _cp(
                    [
                        "git",
                        "diff",
                        "--cached",
                        "--name-only",
                        "--",
                        ".software_factory_bootstrap_state.json",
                    ]
                ),
            ),
            (
                ["git", "diff", "--cached", "--quiet"],
                _cp(["git", "diff", "--cached", "--quiet"], returncode=0),
            ),
        ],
    )

    result = git_ops.commit_and_push("/repo", "msg")
    assert result == {
        "success": False,
        "commit_sha": None,
        "error": "no_changes",
        "error_stage": "git_diff",
        "remote": "origin",
        "branch": None,
        "pushed_ref": None,
    }
    assert calls == [
        ["git", "add", "-A"],
        [
            "git",
            "diff",
            "--cached",
            "--name-only",
            "--",
            ".software_factory_bootstrap_state.json",
        ],
        ["git", "diff", "--cached", "--quiet"],
    ]


def test_commit_and_push_success_infers_current_branch(monkeypatch) -> None:
    calls = _patch_run(
        monkeypatch,
        [
            (["git", "add", "-A"], _cp(["git", "add", "-A"])),
            (
                [
                    "git",
                    "diff",
                    "--cached",
                    "--name-only",
                    "--",
                    ".software_factory_bootstrap_state.json",
                ],
                _cp(
                    [
                        "git",
                        "diff",
                        "--cached",
                        "--name-only",
                        "--",
                        ".software_factory_bootstrap_state.json",
                    ]
                ),
            ),
            (
                ["git", "diff", "--cached", "--quiet"],
                _cp(["git", "diff", "--cached", "--quiet"], returncode=1),
            ),
            (
                ["git", "commit", "-m", "feat: m5"],
                _cp(["git", "commit", "-m", "feat: m5"]),
            ),
            (
                ["git", "rev-parse", "HEAD"],
                _cp(["git", "rev-parse", "HEAD"], stdout="deadbeef\n"),
            ),
            (
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                _cp(
                    ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                    stdout="feature/m5\n",
                ),
            ),
            (
                ["git", "push", "origin", "feature/m5"],
                _cp(["git", "push", "origin", "feature/m5"]),
            ),
        ],
    )

    result = git_ops.commit_and_push("/repo", "feat: m5")
    assert result == {
        "success": True,
        "commit_sha": "deadbeef",
        "error": None,
        "error_stage": None,
        "remote": "origin",
        "branch": "feature/m5",
        "pushed_ref": "origin/feature/m5",
    }
    assert calls[-1] == ["git", "push", "origin", "feature/m5"]


def test_commit_and_push_push_failure_uses_given_branch(monkeypatch) -> None:
    calls = _patch_run(
        monkeypatch,
        [
            (["git", "add", "-A"], _cp(["git", "add", "-A"])),
            (
                [
                    "git",
                    "diff",
                    "--cached",
                    "--name-only",
                    "--",
                    ".software_factory_bootstrap_state.json",
                ],
                _cp(
                    [
                        "git",
                        "diff",
                        "--cached",
                        "--name-only",
                        "--",
                        ".software_factory_bootstrap_state.json",
                    ]
                ),
            ),
            (
                ["git", "diff", "--cached", "--quiet"],
                _cp(["git", "diff", "--cached", "--quiet"], returncode=1),
            ),
            (
                ["git", "commit", "-m", "feat: m5"],
                _cp(["git", "commit", "-m", "feat: m5"]),
            ),
            (
                ["git", "rev-parse", "HEAD"],
                _cp(["git", "rev-parse", "HEAD"], stdout="deadbeef\n"),
            ),
            (
                ["git", "push", "upstream", "release/m5"],
                _cp(
                    ["git", "push", "upstream", "release/m5"],
                    returncode=1,
                    stderr="rejected\n",
                ),
            ),
        ],
    )

    result = git_ops.commit_and_push(
        "/repo",
        "feat: m5",
        remote="upstream",
        branch="release/m5",
    )
    assert result == {
        "success": False,
        "commit_sha": "deadbeef",
        "error": "rejected",
        "error_stage": "git_push",
        "remote": "upstream",
        "branch": "release/m5",
        "pushed_ref": "upstream/release/m5",
    }
    assert ["git", "rev-parse", "--abbrev-ref", "HEAD"] not in calls


def test_commit_and_push_excludes_runtime_state_file(monkeypatch) -> None:
    calls = _patch_run(
        monkeypatch,
        [
            (["git", "add", "-A"], _cp(["git", "add", "-A"])),
            (
                [
                    "git",
                    "diff",
                    "--cached",
                    "--name-only",
                    "--",
                    ".software_factory_bootstrap_state.json",
                ],
                _cp(
                    [
                        "git",
                        "diff",
                        "--cached",
                        "--name-only",
                        "--",
                        ".software_factory_bootstrap_state.json",
                    ],
                    stdout=".software_factory_bootstrap_state.json\n",
                ),
            ),
            (
                [
                    "git",
                    "reset",
                    "--quiet",
                    "HEAD",
                    "--",
                    ".software_factory_bootstrap_state.json",
                ],
                _cp(
                    [
                        "git",
                        "reset",
                        "--quiet",
                        "HEAD",
                        "--",
                        ".software_factory_bootstrap_state.json",
                    ]
                ),
            ),
            (
                ["git", "diff", "--cached", "--quiet"],
                _cp(["git", "diff", "--cached", "--quiet"], returncode=0),
            ),
        ],
    )

    result = git_ops.commit_and_push("/repo", "msg")

    assert result["error"] == "no_changes"
    assert [
        "git",
        "reset",
        "--quiet",
        "HEAD",
        "--",
        ".software_factory_bootstrap_state.json",
    ] in calls


def test_post_pr_comment_success(monkeypatch) -> None:
    _patch_run(
        monkeypatch,
        [
            (
                [
                    "gh",
                    "pr",
                    "comment",
                    "45",
                    "--repo",
                    "acme/widgets",
                    "--body",
                    "done",
                ],
                _cp(["gh"], stdout="https://example.test/comment/1\n"),
            )
        ],
    )

    ok, message = git_ops.post_pr_comment(
        repo_dir="/repo",
        repo="acme/widgets",
        pr_number=45,
        body="done",
    )
    assert ok is True
    assert "comment" in message


def test_post_pr_comment_failure(monkeypatch) -> None:
    _patch_run(
        monkeypatch,
        [
            (
                [
                    "gh",
                    "pr",
                    "comment",
                    "45",
                    "--repo",
                    "acme/widgets",
                    "--body",
                    "done",
                ],
                _cp(["gh"], returncode=1, stderr="not authorized\n"),
            )
        ],
    )

    ok, message = git_ops.post_pr_comment(
        repo_dir="/repo",
        repo="acme/widgets",
        pr_number=45,
        body="done",
    )
    assert ok is False
    assert "authorized" in message
