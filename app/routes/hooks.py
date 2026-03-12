from typing import Any

from fastapi import APIRouter

from app.schemas.hooks import HookEvent
from app.services.hooks import process_hook_event


router = APIRouter(tags=["hooks"])


@router.post("/hook-events")
async def hook_events(event: HookEvent) -> dict[str, Any]:
    normalized_event = event.model_dump(mode="json")
    event_type = event.event
    process_result = process_hook_event(normalized_event, event_type)
    return {
        "ok": True,
        "message": "Hook event received",
        "event_type": event_type,
        "process_result": process_result,
        "received": normalized_event,
    }
