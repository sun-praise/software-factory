from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path

from app.db import connect_db
from app.services.feature_flags import (
    build_feature_flag_context,
    save_agent_feature_flags,
)

from app.db import connect_db
from app.services.feature_flags import (
    build_feature_flag_context,
    save_agent_feature_flags,
)


router = APIRouter(tags=["web"])


def _fetch_runs(limit: int = 20) -> list[dict[str, str]]:
    with connect_db() as conn:
        rows = conn.execute(
            """
            SELECT id, repo, pr_number, status, created_at, updated_at
            FROM autofix_runs
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    return [
        {
            "id": str(row["id"]),
            "repo": str(row["repo"]),
            "pr_number": str(row["pr_number"]),
            "status": str(row["status"]),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }
        for row in rows
    ]


def _read_log_preview(logs_path: str | None, max_chars: int = 1200) -> str:
    if not logs_path:
        return "No log data yet."
    path = Path(logs_path)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "No log data yet."
    return text.strip()[:max_chars] or "No log data yet."


@router.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    templates: Jinja2Templates = request.app.state.templates
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "request": request,
            "title": "Software Factory",
            "runs": _fetch_runs(),
        },
    )


@router.get("/runs", response_class=HTMLResponse)
async def runs_page(request: Request) -> HTMLResponse:
    return await index(request)


@router.get("/runs/{run_id}", response_class=HTMLResponse)
async def run_detail(request: Request, run_id: str) -> HTMLResponse:
    templates: Jinja2Templates = request.app.state.templates
    try:
        run_id_value = int(run_id)
    except ValueError:
        run_id_value = -1

    with connect_db() as conn:
        row = conn.execute(
            """
            SELECT id, status, created_at, updated_at, logs_path
            FROM autofix_runs
            WHERE id = ?
            """,
            (run_id_value,),
        ).fetchone()

    run = {
        "id": run_id,
        "status": "not_found",
        "created_at": "-",
        "updated_at": "-",
        "log_preview": "No log data yet.",
    }
    if row is not None:
        run = {
            "id": str(row["id"]),
            "status": str(row["status"]),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
            "log_preview": _read_log_preview(row["logs_path"]),
        }
    return templates.TemplateResponse(
        request=request,
        name="run_detail.html",
        context={"request": request, "run": run},
    )


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request) -> HTMLResponse:
    templates: Jinja2Templates = request.app.state.templates
    with connect_db() as conn:
        flag_context = build_feature_flag_context(conn)
    return templates.TemplateResponse(
        request=request,
        name="settings.html",
        context={
            "request": request,
            "title": "Software Factory - Settings",
            "saved": request.query_params.get("saved") == "1",
            **flag_context,
        },
    )


@router.post("/settings", response_class=HTMLResponse)
async def save_settings(request: Request) -> RedirectResponse:
    form = await request.form()
    openhands_enabled = "agent_openhands_enabled" in form
    legacy_enabled = "agent_legacy_enabled" in form

    openhands_command = str(form.get("openhands_command", "openhands")).strip()
    openhands_worktree_base_dir = str(
        form.get("openhands_worktree_base_dir", ".software-factory-worktrees")
    ).strip()
    timeout_raw = str(form.get("openhands_command_timeout_seconds", "600"))
    try:
        openhands_command_timeout_seconds = max(1, int(timeout_raw.strip()))
    except (TypeError, ValueError):
        openhands_command_timeout_seconds = 600

    with connect_db() as conn:
        save_agent_feature_flags(
            conn,
            openhands_enabled=openhands_enabled,
            legacy_enabled=legacy_enabled,
            openhands_command=openhands_command,
            openhands_command_timeout_seconds=openhands_command_timeout_seconds,
            openhands_worktree_base_dir=openhands_worktree_base_dir,
        )

    return RedirectResponse(url="/settings?saved=1", status_code=303)
