from __future__ import annotations

import logging
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Cookie, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from app.config import get_settings
from app.db import connect_db
from app.providers import get_oauth_provider

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

_SESSION_COOKIE_NAME = "sf_session"
_STATE_COOKIE_NAME = "sf_oauth_state"
_SESSION_TTL_DAYS = 30


def _generate_state() -> str:
    return secrets.token_urlsafe(32)


def _generate_session_token() -> str:
    return secrets.token_urlsafe(48)


def _build_redirect_uri(request: Request) -> str:
    settings = get_settings()
    if settings.oauth_redirect_uri:
        return settings.oauth_redirect_uri
    base_url = str(request.base_url).rstrip("/")
    return f"{base_url}/auth/callback"


def _get_current_user(conn: sqlite3.Connection, session_token: str) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT u.id, u.provider, u.provider_user_id, u.username,
               u.display_name, u.email, u.avatar_url
        FROM users u
        JOIN oauth_sessions os ON os.user_id = u.id
        WHERE os.session_token = ? AND os.expires_at > ?
        """,
        (session_token, datetime.now(timezone.utc).isoformat()),
    ).fetchone()
    if not row:
        return None
    return {
        "id": row[0],
        "provider": row[1],
        "provider_user_id": row[2],
        "username": row[3],
        "display_name": row[4],
        "email": row[5],
        "avatar_url": row[6],
    }


def _upsert_user(
    conn: sqlite3.Connection,
    *,
    provider: str,
    provider_user_id: str,
    username: str,
    display_name: str,
    email: str | None,
    avatar_url: str | None,
) -> int:
    existing = conn.execute(
        "SELECT id FROM users WHERE provider = ? AND provider_user_id = ?",
        (provider, provider_user_id),
    ).fetchone()
    if existing:
        conn.execute(
            """
            UPDATE users SET username = ?, display_name = ?, email = ?,
                             avatar_url = ?, updated_at = ?
            WHERE id = ?
            """,
            (username, display_name, email, avatar_url,
             datetime.now(timezone.utc).isoformat(), existing[0]),
        )
        return existing[0]
    cursor = conn.execute(
        """
        INSERT INTO users (provider, provider_user_id, username, display_name,
                           email, avatar_url)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (provider, provider_user_id, username, display_name, email, avatar_url),
    )
    return cursor.lastrowid


def _create_session(
    conn: sqlite3.Connection,
    *,
    user_id: int,
    provider: str,
    access_token: str,
) -> str:
    token = _generate_session_token()
    expires_at = datetime.now(timezone.utc) + timedelta(days=_SESSION_TTL_DAYS)
    conn.execute(
        """
        INSERT INTO oauth_sessions (session_token, user_id, provider,
                                    access_token, expires_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (token, user_id, provider, access_token, expires_at.isoformat()),
    )
    return token


@router.get("/login")
async def login(request: Request) -> Response:
    settings = get_settings()
    if not settings.github_oauth_client_id:
        return HTMLResponse(
            "<h1>OAuth not configured</h1>"
            "<p>Set GITHUB_OAUTH_CLIENT_ID and GITHUB_OAUTH_CLIENT_SECRET to enable login.</p>",
            status_code=503,
        )

    provider = get_oauth_provider()
    state = _generate_state()
    redirect_uri = _build_redirect_uri(request)
    authorize_url = provider.build_authorize_url(
        client_id=settings.github_oauth_client_id,
        redirect_uri=redirect_uri,
        state=state,
    )

    response = RedirectResponse(url=authorize_url, status_code=302)
    response.set_cookie(
        key=_STATE_COOKIE_NAME,
        value=state,
        httponly=True,
        max_age=600,
        samesite="lax",
    )
    return response


@router.get("/callback")
async def callback(
    request: Request,
    code: str = "",
    state: str = "",
) -> Response:
    settings = get_settings()
    cookie_state = request.cookies.get(_STATE_COOKIE_NAME)

    if not code or not state:
        return HTMLResponse("<h1>Authentication failed</h1><p>Missing code or state.</p>", status_code=400)

    if cookie_state != state:
        return HTMLResponse("<h1>Authentication failed</h1><p>State mismatch.</p>", status_code=400)

    provider = get_oauth_provider()
    redirect_uri = _build_redirect_uri(request)

    try:
        token_data = await provider.exchange_code(
            client_id=settings.github_oauth_client_id,
            client_secret=settings.github_oauth_client_secret,
            code=code,
            redirect_uri=redirect_uri,
        )
    except Exception:
        logger.exception("OAuth token exchange failed")
        return HTMLResponse("<h1>Authentication failed</h1><p>Token exchange error.</p>", status_code=502)

    access_token = token_data.get("access_token", "")
    if not access_token:
        return HTMLResponse("<h1>Authentication failed</h1><p>No access token received.</p>", status_code=502)

    try:
        user_info = await provider.fetch_user_info(access_token=access_token)
    except Exception:
        logger.exception("OAuth user info fetch failed")
        return HTMLResponse("<h1>Authentication failed</h1><p>Could not fetch user info.</p>", status_code=502)

    with connect_db() as conn:
        user_id = _upsert_user(
            conn,
            provider=user_info.provider,
            provider_user_id=user_info.provider_user_id,
            username=user_info.username,
            display_name=user_info.display_name,
            email=user_info.email,
            avatar_url=user_info.avatar_url,
        )
        session_token = _create_session(
            conn,
            user_id=user_id,
            provider=user_info.provider,
            access_token=access_token,
        )
        conn.commit()

    response = RedirectResponse(url="/", status_code=302)
    response.set_cookie(
        key=_SESSION_COOKIE_NAME,
        value=session_token,
        httponly=True,
        max_age=_SESSION_TTL_DAYS * 86400,
        samesite="lax",
    )
    response.delete_cookie(key=_STATE_COOKIE_NAME)
    return response


@router.get("/logout")
async def logout(
    request: Request,
    sf_session: str | None = Cookie(default=None, alias=_SESSION_COOKIE_NAME),
) -> Response:
    if sf_session:
        with connect_db() as conn:
            conn.execute(
                "DELETE FROM oauth_sessions WHERE session_token = ?",
                (sf_session,),
            )
            conn.commit()

    response = RedirectResponse(url="/", status_code=302)
    response.delete_cookie(key=_SESSION_COOKIE_NAME)
    return response


def get_current_user_from_request(
    request: Request,
) -> dict[str, Any] | None:
    session_token = request.cookies.get(_SESSION_COOKIE_NAME)
    if not session_token:
        return None
    with connect_db() as conn:
        return _get_current_user(conn, session_token)
