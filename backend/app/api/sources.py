"""CRUD for tracked discovery sources (seasons, districts, teams)."""

from __future__ import annotations

import re
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from ..models import Source, SourceKind
from ..services.scanner import FIRST_FRC_YEAR
from .deps import get_session

router = APIRouter(tags=["sources"])


class SourceIn(BaseModel):
    kind: SourceKind
    value: str
    notes: str = ""
    start_year: Optional[int] = None
    end_year: Optional[int] = None


def _normalize(kind: SourceKind, value: str) -> str:
    value = value.strip().lower()
    if kind == SourceKind.team and value.isdigit():
        return f"frc{value}"
    if kind == SourceKind.event_type:
        nums = sorted({int(t) for t in re.findall(r"\d+", value)})
        return ",".join(str(n) for n in nums)
    return value


def _validate(kind: SourceKind, value: str) -> None:
    if kind == SourceKind.season and not re.fullmatch(r"\d{4}", value):
        raise HTTPException(400, "Season must be a 4-digit year, e.g. 2026")
    if kind == SourceKind.team and not re.fullmatch(r"frc\d+", value):
        raise HTTPException(400, "Team must be like 254 or frc254")
    if kind == SourceKind.district and not re.fullmatch(r"\d{4}[a-z]+", value):
        raise HTTPException(400, "District must be like 2026ne")
    if kind == SourceKind.event_type and not re.fullmatch(r"\d+(,\d+)*", value):
        raise HTTPException(400, "Event type must be like 3,4,5,6")


def _validate_years(
    kind: SourceKind, start_year: Optional[int], end_year: Optional[int]
) -> None:
    if kind != SourceKind.team:
        if start_year is not None or end_year is not None:
            raise HTTPException(400, "Only team sources take a year range")
        return
    now = date.today().year
    start = start_year or FIRST_FRC_YEAR
    if not FIRST_FRC_YEAR <= start <= now:
        raise HTTPException(
            400, f"Start year must be between {FIRST_FRC_YEAR} and {now}"
        )
    if end_year is not None and not start <= end_year <= now:
        raise HTTPException(400, f"End year must be between {start} and {now}")


@router.get("/sources")
def list_sources(session: Session = Depends(get_session)):
    return session.exec(select(Source).order_by(Source.kind, Source.value)).all()


@router.post("/sources")
def create_source(payload: SourceIn, session: Session = Depends(get_session)):
    value = _normalize(payload.kind, payload.value)
    _validate(payload.kind, value)
    _validate_years(payload.kind, payload.start_year, payload.end_year)
    existing = session.exec(
        select(Source).where(Source.kind == payload.kind, Source.value == value)
    ).first()
    if existing:
        raise HTTPException(409, "That source already exists")
    start_year = None
    if payload.kind == SourceKind.team:
        start_year = payload.start_year or FIRST_FRC_YEAR
    source = Source(
        kind=payload.kind,
        value=value,
        notes=payload.notes,
        start_year=start_year,
        end_year=payload.end_year,
    )
    session.add(source)
    session.commit()
    session.refresh(source)
    return source


@router.patch("/sources/{source_id}")
def update_source(
    source_id: int,
    enabled: Optional[bool] = None,
    start_year: Optional[int] = None,
    end_year: Optional[int] = None,
    ongoing: Optional[bool] = None,
    session: Session = Depends(get_session),
):
    source = session.get(Source, source_id)
    if source is None:
        raise HTTPException(404, "Source not found")
    if enabled is not None:
        source.enabled = enabled
    if start_year is not None or end_year is not None or ongoing is not None:
        new_start = start_year if start_year is not None else source.start_year
        new_end = (
            None if ongoing else (end_year if end_year is not None else source.end_year)
        )
        _validate_years(source.kind, new_start, new_end)
        source.start_year = new_start
        source.end_year = new_end
    session.add(source)
    session.commit()
    session.refresh(source)
    return source


@router.delete("/sources/{source_id}")
def delete_source(source_id: int, session: Session = Depends(get_session)):
    source = session.get(Source, source_id)
    if source is None:
        raise HTTPException(404, "Source not found")
    session.delete(source)
    session.commit()
    return {"deleted": True}
