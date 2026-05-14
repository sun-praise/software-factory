from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.db import connect_db
from app.services.byok import (
    SUPPORTED_PROVIDERS,
    UserApiKeyCreatePayload,
    _PROVIDER_LABELS,
    add_api_key,
    delete_api_key,
    list_api_keys,
    toggle_api_key,
)

router = APIRouter(tags=["byok"])

_PROVIDERS = [
    {"value": p, "label": _PROVIDER_LABELS.get(p, p)}
    for p in SUPPORTED_PROVIDERS
]


@router.get("/byok", response_class=HTMLResponse)
async def byok_page(request: Request) -> HTMLResponse:
    templates = request.app.state.templates
    with connect_db() as conn:
        keys = list_api_keys(conn)
    return templates.TemplateResponse(
        request=request,
        name="byok.html",
        context={
            "request": request,
            "title": "API Keys (BYOK)",
            "keys": keys,
            "providers": _PROVIDERS,
            "message": None,
            "message_class": "ok",
            "form": {},
        },
    )


@router.post("/byok", response_class=HTMLResponse)
async def byok_add_key(request: Request) -> HTMLResponse:
    templates = request.app.state.templates
    form = await request.form()
    provider = str(form.get("provider", "")).strip()
    api_key = str(form.get("api_key", "")).strip()
    label = str(form.get("label", "")).strip()

    try:
        payload = UserApiKeyCreatePayload(provider=provider, api_key=api_key, label=label)
    except Exception:
        return templates.TemplateResponse(
            request=request,
            name="byok.html",
            context={
                "request": request,
                "title": "API Keys (BYOK)",
                "keys": _load_keys(),
                "providers": _PROVIDERS,
                "message": "Invalid input.",
                "message_class": "",
                "form": {"provider": provider, "label": label},
            },
            status_code=400,
        )

    try:
        with connect_db() as conn:
            add_api_key(conn, payload)
    except ValueError as exc:
        return templates.TemplateResponse(
            request=request,
            name="byok.html",
            context={
                "request": request,
                "title": "API Keys (BYOK)",
                "keys": _load_keys(),
                "providers": _PROVIDERS,
                "message": str(exc),
                "message_class": "",
                "form": {"provider": provider, "label": label},
            },
            status_code=400,
        )

    return RedirectResponse(url="/byok?added=1", status_code=303)


@router.post("/byok/{key_id}/toggle", response_class=HTMLResponse)
async def byok_toggle_key(request: Request, key_id: int) -> HTMLResponse:
    form = await request.form()
    enabled = form.get("enabled") != "false"
    with connect_db() as conn:
        updated = toggle_api_key(conn, key_id, enabled=not enabled)
    if not updated:
        raise HTTPException(status_code=404, detail="Key not found")
    return RedirectResponse(url="/byok", status_code=303)


@router.post("/byok/{key_id}/delete", response_class=HTMLResponse)
async def byok_delete_key(request: Request, key_id: int) -> HTMLResponse:
    with connect_db() as conn:
        deleted = delete_api_key(conn, key_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Key not found")
    return RedirectResponse(url="/byok", status_code=303)


@router.get("/api/byok/keys")
async def api_list_keys() -> JSONResponse:
    with connect_db() as conn:
        keys = list_api_keys(conn)
    return JSONResponse(
        {
            "keys": [
                {
                    "id": k.id,
                    "provider": k.provider,
                    "label": k.label,
                    "masked_key": k.masked_key,
                    "enabled": k.enabled,
                    "created_at": k.created_at,
                }
                for k in keys
            ]
        }
    )


@router.post("/api/byok/keys")
async def api_add_key(request: Request) -> JSONResponse:
    body = await request.json()
    provider = str(body.get("provider", "")).strip()
    api_key = str(body.get("api_key", "")).strip()
    label = str(body.get("label", "")).strip()

    try:
        payload = UserApiKeyCreatePayload(provider=provider, api_key=api_key, label=label)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc

    try:
        with connect_db() as conn:
            entry = add_api_key(conn, payload)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc

    return JSONResponse(
        {
            "ok": True,
            "key": {
                "id": entry.id,
                "provider": entry.provider,
                "label": entry.label,
                "masked_key": entry.masked_key,
                "enabled": entry.enabled,
            },
        },
        status_code=201,
    )


@router.delete("/api/byok/keys/{key_id}")
async def api_delete_key(key_id: int) -> JSONResponse:
    with connect_db() as conn:
        deleted = delete_api_key(conn, key_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Key not found")
    return JSONResponse({"ok": True})


def _load_keys():
    with connect_db() as conn:
        return list_api_keys(conn)
