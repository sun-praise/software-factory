from __future__ import annotations

from app.services.ai_client import (
    FileChange,
    _build_request_prompt,
    _parse_fix_plan,
    _read_context_file,
)


def test_parse_fix_plan_accepts_language_fenced_json() -> None:
    plan = _parse_fix_plan(
        """```python
        {
          \"summary\": \"fixed formatting\",
          \"changes\": [
            {\"path\": \"app/main.py\", \"action\": \"write\", \"content\": \"print('ok')\\n\"}
          ]
        }
        ```"""
    )

    assert plan.summary == "fixed formatting"
    assert plan.changes == (
        FileChange(path="app/main.py", action="write", content="print('ok')\n"),
    )


def test_parse_fix_plan_accepts_fenced_json() -> None:
    plan = _parse_fix_plan(
        """```json
        {
          \"summary\": \"fixed bug\",
          \"changes\": [
            {\"path\": \"app/main.py\", \"action\": \"write\", \"content\": \"print('ok')\\n\"}
          ]
        }
        ```"""
    )

    assert plan.summary == "fixed bug"
    assert plan.changes == (
        FileChange(path="app/main.py", action="write", content="print('ok')\n"),
    )


def test_parse_fix_plan_parses_json_inside_free_text() -> None:
    plan = _parse_fix_plan(
        """
        Hi team,
        ```python
        {
          \"summary\": \"extra text\",
          \"changes\": []
        }
        ```
        Thanks.
        """
    )

    assert plan.summary == "extra text"


def test_build_request_prompt_includes_referenced_file_contents(tmp_path) -> None:
    target = tmp_path / "app" / "main.py"
    target.parent.mkdir(parents=True)
    target.write_text("print('hello')\n", encoding="utf-8")

    prompt = _build_request_prompt(
        prompt="fix the bug",
        workspace_dir=str(tmp_path),
        normalized_review={"must_fix": [{"path": "app/main.py"}]},
    )

    assert "Relevant file snapshots:" in prompt
    assert "--- app/main.py ---" in prompt
    assert "print('hello')" in prompt


def test_build_request_prompt_does_not_follow_symlinked_context_file(tmp_path) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    linked_target = tmp_path / "outside.txt"
    linked_target.write_text("outside secret", encoding="utf-8")
    (workspace / "outside.txt").symlink_to(linked_target)

    prompt = _build_request_prompt(
        prompt="fix the bug",
        workspace_dir=str(workspace),
        normalized_review={"must_fix": [{"path": "outside.txt"}]},
    )

    assert "--- outside.txt (missing) ---" in prompt


def test_read_context_file_rejects_traversal_path(tmp_path) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / "main.py").write_text("ok\n", encoding="utf-8")

    assert _read_context_file(workspace, "../main.py") is None
