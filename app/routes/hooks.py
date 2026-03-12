from fastapi import APIRouter

from app.schemas.hooks import HookEvent


router = APIRouter(tags=["hooks"])


@router.post("/hook-events")
async def hook_events(event: HookEvent) -> dict[str, object]:
    normalized_event = event.model_dump(mode="json")
    return {
        "ok": True,
        "message": "Hook event received",
        "event_type": event.event,
        "event": normalized_event,
        "received": normalized_event,
    }
