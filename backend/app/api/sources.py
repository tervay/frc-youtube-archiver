"""CRUD for tracked discovery sources (seasons, districts, teams)."""
from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from ..models import Source, SourceKind
from .deps import get_session

router = APIRouter(tags=["sources"])


class SourceIn(BaseModel):
    kind: SourceKind
    value: str
    notes: str = ""


def _normalize(kind: SourceKind, value: str) -> str:
    value = value.strip().lower()
    if kind == SourceKind.team and value.isdigit():
        return f"frc{value}"
    return value


def _validate(kind: SourceKind, value: str) -> None:
    if kind == SourceKind.season and not re.fullmatch(r"\d{4}", value):
        raise HTTPException(400, "Season must be a 4-digit year, e.g. 2026")
    if kind == SourceKind.team and not re.fullmatch(r"frc\d+", value):
        raise HTTPException(400, "Team must be like 254 or frc254")
    if kind == SourceKind.district and not re.fullmatch(r"\d{4}[a-z]+", value):
        raise HTTPException(400, "District must be like 2026ne")


@router.get("/sources")
def list_sources(session: Session = Depends(get_session)):
    return session.exec(select(Source).order_by(Source.kind, Source.value)).all()


@router.post("/sources")
def create_source(payload: SourceIn, session: Session = Depends(get_session)):
    value = _normalize(payload.kind, payload.value)
    _validate(payload.kind, value)
    existing = session.exec(
        select(Source).where(Source.kind == payload.kind, Source.value == value)
    ).first()
    if existing:
        raise HTTPException(409, "That source already exists")
    source = Source(kind=payload.kind, value=value, notes=payload.notes)
    session.add(source)
    session.commit()
    session.refresh(source)
    return source


@router.patch("/sources/{source_id}")
def toggle_source(source_id: int, enabled: bool,
                  session: Session = Depends(get_session)):
    source = session.get(Source, source_id)
    if source is None:
        raise HTTPException(404, "Source not found")
    source.enabled = enabled
    session.add(source)
    session.commit()
    return source


@router.delete("/sources/{source_id}")
def delete_source(source_id: int, session: Session = Depends(get_session)):
    source = session.get(Source, source_id)
    if source is None:
        raise HTTPException(404, "Source not found")
    session.delete(source)
    session.commit()
    return {"deleted": True}
