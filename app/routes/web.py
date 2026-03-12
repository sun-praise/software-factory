from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates


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
