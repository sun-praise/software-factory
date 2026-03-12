from typing import Any

from fastapi import APIRouter, Request


router = APIRouter(tags=["hooks"])


async def _read_payload(request: Request) -> dict[str, Any]:
    try:
        data = await request.json()
    except Exception:
        return {}
    return data if isinstance(data, dict) else {"raw": data}


@router.post("/hook-events")
async def hook_events(request: Request) -> dict[str, Any]:
    payload = await _read_payload(request)
    event_type = request.headers.get("x-event-type", "unknown")
    return {
        "ok": True,
        "message": "Hook event received",
        "event_type": event_type,
        "received": payload,
    }
