from __future__ import annotations

from app.services.ai_client import FileChange, _build_request_prompt, _parse_fix_plan


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
