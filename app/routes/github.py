from typing import Any

from fastapi import APIRouter, Request


router = APIRouter(prefix="/github", tags=["github"])


async def _read_payload(request: Request) -> dict[str, Any]:
    try:
        data = await request.json()
    except Exception:
        return {}
    return data if isinstance(data, dict) else {"raw": data}


@router.post("/webhook")
async def github_webhook(request: Request) -> dict[str, Any]:
    payload = await _read_payload(request)
    event_type = request.headers.get("x-github-event", "unknown")
    return {
        "ok": True,
        "message": "GitHub webhook received",
        "event_type": event_type,
        "received": payload,
    }
