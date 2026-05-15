from __future__ import annotations

import hmac

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.config import get_settings
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


async def _verify_byok_admin(request: Request) -> None:
    settings = get_settings()
    admin_token = settings.byok_admin_token
    if not admin_token:
        return
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        token = auth[len("Bearer "):]
    else:
        token = auth
    if not hmac.compare_digest(token, admin_token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing BYOK admin token",
        )


def _check_html_admin(request: Request) -> bool:
    settings = get_settings()
    admin_token = settings.byok_admin_token
    if not admin_token:
        return True
    token = request.cookies.get("byok_admin_token", "")
    return hmac.compare_digest(token, admin_token)


def _html_auth_response(request: Request):
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="BYOK admin token required. Access via /byok?token=<your_token>",
    )


@router.get("/byok", response_class=HTMLResponse)
async def byok_page(request: Request) -> HTMLResponse:
    settings = get_settings()
    admin_token = settings.byok_admin_token
    if admin_token:
        qp_token = request.query_params.get("token", "")
        cookie_token = request.cookies.get("byok_admin_token", "")
        qp_valid = hmac.compare_digest(qp_token, admin_token) if qp_token else False
        cookie_valid = hmac.compare_digest(cookie_token, admin_token) if cookie_token else False
        if not (cookie_valid or qp_valid):
            _html_auth_response(request)

    templates = request.app.state.templates
    added = request.query_params.get("added") == "1"
    with connect_db() as conn:
        keys = list_api_keys(conn)
    message = "API key added successfully." if added else None
    response = templates.TemplateResponse(
        request=request,
        name="byok.html",
        context={
            "request": request,
            "title": "API Keys (BYOK)",
            "keys": keys,
            "providers": _PROVIDERS,
            "message": message,
            "message_class": "ok" if added else "",
            "form": {},
        },
    )
    if admin_token:
        qp_token = request.query_params.get("token", "")
        if qp_token and hmac.compare_digest(qp_token, admin_token):
            response.set_cookie(
                "byok_admin_token", admin_token, httponly=True, max_age=86400
            )
    return response


@router.post("/byok", response_class=HTMLResponse)
async def byok_add_key(request: Request) -> HTMLResponse:
    if not _check_html_admin(request):
        _html_auth_response(request)
    templates = request.app.state.templates
    form = await request.form()
    provider = str(form.get("provider", "")).strip()
    api_key = str(form.get("api_key", "")).strip()
    label = str(form.get("label", "")).strip()

    payload = UserApiKeyCreatePayload(provider=provider, api_key=api_key, label=label)

    try:
        with connect_db() as conn:
            add_api_key(conn, payload)
    except ValueError as exc:
        with connect_db() as conn2:
            keys = _load_keys(conn2)
        return templates.TemplateResponse(
            request=request,
            name="byok.html",
            context={
                "request": request,
                "title": "API Keys (BYOK)",
                "keys": keys,
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
    if not _check_html_admin(request):
        _html_auth_response(request)
    with connect_db() as conn:
        keys = list_api_keys(conn)
        current = next((k for k in keys if k.id == key_id), None)
        if current is None:
            raise HTTPException(status_code=404, detail="Key not found")
        toggle_api_key(conn, key_id, enabled=not current.enabled)
    return RedirectResponse(url="/byok", status_code=303)


@router.post("/byok/{key_id}/delete", response_class=HTMLResponse)
async def byok_delete_key(request: Request, key_id: int) -> HTMLResponse:
    if not _check_html_admin(request):
        _html_auth_response(request)
    with connect_db() as conn:
        deleted = delete_api_key(conn, key_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Key not found")
    return RedirectResponse(url="/byok", status_code=303)


@router.get("/api/byok/keys", dependencies=[Depends(_verify_byok_admin)])
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


@router.post("/api/byok/keys", dependencies=[Depends(_verify_byok_admin)])
async def api_add_key(request: Request) -> JSONResponse:
    body = await request.json()
    provider = str(body.get("provider", "")).strip()
    api_key = str(body.get("api_key", "")).strip()
    label = str(body.get("label", "")).strip()

    payload = UserApiKeyCreatePayload(provider=provider, api_key=api_key, label=label)

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


@router.delete(
    "/api/byok/keys/{key_id}",
    response_class=JSONResponse,
    dependencies=[Depends(_verify_byok_admin)],
)
async def api_delete_key(key_id: int) -> JSONResponse:
    with connect_db() as conn:
        deleted = delete_api_key(conn, key_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Key not found")
    return JSONResponse({"ok": True})


def _load_keys(conn):
    return list_api_keys(conn)
