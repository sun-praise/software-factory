from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.routes.github import router as github_router
from app.routes.hooks import router as hooks_router
from app.routes.web import router as web_router


BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
TEMPLATES_DIR = BASE_DIR / "templates"

app = FastAPI(title="Software Factory")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
app.state.templates = templates

app.include_router(hooks_router)
app.include_router(github_router)
app.include_router(web_router)


@app.get("/healthz")
async def healthz() -> dict[str, bool]:
    return {"ok": True}
