"""Settings endpoints: expose the schema + current values, and save edits."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlmodel import Session

from ..db import get_all_settings, set_setting
from ..logging_config import setup_logging
from ..settings_defaults import SETTINGS
from .. import scheduler
from .deps import get_session

router = APIRouter(tags=["settings"])


class SettingsUpdate(BaseModel):
    values: dict[str, Any]


@router.get("/settings")
def read_settings(session: Session = Depends(get_session)):
    values = get_all_settings(session)
    schema = [
        {
            "key": s.key,
            "label": s.label,
            "help": s.help,
            "type": s.type,
            "group": s.group,
            # never echo the API key back to the browser in cleartext
            "value": ("" if s.type == "password" and values[s.key] else values[s.key]),
            "is_set": bool(values[s.key]) if s.type == "password" else None,
        }
        for s in SETTINGS
    ]
    return {"schema": schema}


@router.put("/settings")
def update_settings(payload: SettingsUpdate, session: Session = Depends(get_session)):
    for key, value in payload.values.items():
        # Ignore blank password fields so we don't wipe a stored secret.
        spec = next((s for s in SETTINGS if s.key == key), None)
        if spec is None:
            continue
        if spec.type == "password" and value == "":
            continue
        set_setting(session, key, value)
    session.commit()
    scheduler.reschedule()  # pick up any cron changes immediately
    setup_logging(get_all_settings(session).get("log_level"))  # apply level now
    return {"saved": True}
