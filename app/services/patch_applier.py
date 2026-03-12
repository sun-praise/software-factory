from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.services.ai_client import FixPlan


@dataclass(frozen=True)
class ApplyResult:
    changed_files: tuple[str, ...]


class PatchApplyError(RuntimeError):
    pass


def apply_fix_plan(*, workspace_dir: str, plan: FixPlan) -> ApplyResult:
    workspace = Path(workspace_dir).expanduser().resolve()
    changed_files: list[str] = []

    for change in plan.changes:
        target = _resolve_target_path(workspace, change.path)
        if change.action == "delete":
            if target.exists():
                if target.is_dir():
                    raise PatchApplyError(
                        f"cannot delete directory path '{change.path}'"
                    )
                target.unlink()
                changed_files.append(change.path)
            continue

        if change.content is None:
            raise PatchApplyError(f"missing content for '{change.path}'")
        target.parent.mkdir(parents=True, exist_ok=True)
        existing = target.read_text(encoding="utf-8") if target.exists() else None
        if existing == change.content:
            continue
        target.write_text(change.content, encoding="utf-8")
        changed_files.append(change.path)

    return ApplyResult(changed_files=tuple(changed_files))


def _resolve_target_path(workspace: Path, relative_path: str) -> Path:
    cleaned = relative_path.strip()
    if not cleaned:
        raise PatchApplyError("change path cannot be empty")
    candidate = (workspace / cleaned).resolve()
    try:
        candidate.relative_to(workspace)
    except ValueError as exc:
        raise PatchApplyError(f"path escapes workspace: {relative_path}") from exc
    return candidate
