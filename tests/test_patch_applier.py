from __future__ import annotations

import pytest

from app.services.ai_client import FileChange, FixPlan
from app.services.patch_applier import PatchApplyError, apply_fix_plan


def test_apply_fix_plan_writes_and_deletes_files(tmp_path) -> None:
    stale = tmp_path / "old.txt"
    stale.write_text("old\n", encoding="utf-8")

    result = apply_fix_plan(
        workspace_dir=str(tmp_path),
        plan=FixPlan(
            summary="update files",
            changes=(
                FileChange(path="src/new.py", content="print('ok')\n"),
                FileChange(path="old.txt", action="delete"),
            ),
        ),
    )

    assert result.changed_files == ("src/new.py", "old.txt")
    assert (tmp_path / "src" / "new.py").read_text(encoding="utf-8") == "print('ok')\n"
    assert not stale.exists()


def test_apply_fix_plan_rejects_paths_outside_workspace(tmp_path) -> None:
    with pytest.raises(PatchApplyError):
        apply_fix_plan(
            workspace_dir=str(tmp_path),
            plan=FixPlan(
                summary="bad path",
                changes=(FileChange(path="../escape.py", content="boom\n"),),
            ),
        )
