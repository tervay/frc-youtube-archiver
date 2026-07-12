"""Manual trigger + status for the Ganymede (Twitch) sync."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends
from sqlmodel import Session

from ..db import get_all_settings
from ..services.ganymede_client import GanymedeClient, GanymedeError
from ..services.jobs import run_ganymede_sync
from .deps import get_session

router = APIRouter(tags=["ganymede"])


@router.post("/ganymede/sync")
async def sync_now():
    run = await asyncio.get_event_loop().run_in_executor(None, run_ganymede_sync)
    return {
        "ok": run.ok,
        "discovered": run.discovered,
        "enqueued": run.enqueued,
        "message": run.message,
    }


@router.get("/ganymede/status")
def status(session: Session = Depends(get_session)):
    settings = get_all_settings(session)
    if not settings["ganymede_enabled"]:
        return {"enabled": False, "reachable": False, "watched": []}
    try:
        client = GanymedeClient(
            settings["ganymede_base_url"], settings["ganymede_api_key"]
        )
        watched = client.list_watched()
        logins = sorted(
            {
                ((entry.get("edges") or {}).get("channel") or {}).get("name")
                for entry in watched
                if ((entry.get("edges") or {}).get("channel") or {}).get("name")
            }
        )
        return {"enabled": True, "reachable": True, "watched": logins}
    except GanymedeError as e:
        return {"enabled": True, "reachable": False, "error": str(e), "watched": []}
