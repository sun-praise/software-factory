from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.db import connect_db
from app.services.feature_flags import (
    build_feature_flag_context,
    save_agent_feature_flags,
)


router = APIRouter(tags=["web"])


def _sample_runs() -> list[dict[str, str]]:
    return [
        {
            "id": "demo-run-001",
            "status": "queued",
            "created_at": "-",
            "updated_at": "-",
        },
        {
            "id": "demo-run-002",
            "status": "running",
            "created_at": "-",
            "updated_at": "-",
        },
    ]


@router.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    templates: Jinja2Templates = request.app.state.templates
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "request": request,
            "title": "Software Factory",
            "runs": _sample_runs(),
        },
    )


@router.get("/runs", response_class=HTMLResponse)
async def runs_page(request: Request) -> HTMLResponse:
    return await index(request)


@router.get("/runs/{run_id}", response_class=HTMLResponse)
async def run_detail(request: Request, run_id: str) -> HTMLResponse:
    templates: Jinja2Templates = request.app.state.templates
    run = {
        "id": run_id,
        "status": "pending",
        "created_at": "-",
        "updated_at": "-",
        "log_preview": "No log data yet.",
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
