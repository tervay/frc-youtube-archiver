from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlmodel import Session, func, select

from ..models import Image, ImageStatus
from .deps import get_session

router = APIRouter(tags=["images"])


@router.get("/images")
def list_images(
    session: Session = Depends(get_session),
    team: Optional[int] = None,
    year: Optional[int] = None,
    status: Optional[ImageStatus] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(60, ge=1, le=200),
):
    filters = []
    if team is not None:
        filters.append(Image.team_number == team)
    if year is not None:
        filters.append(Image.year == year)

    counts = dict(
        session.exec(
            select(Image.status, func.count()).where(*filters).group_by(Image.status)
        ).all()
    )

    if status is not None:
        filters.append(Image.status == status)
    total = session.exec(select(func.count()).select_from(Image).where(*filters)).one()
    rows = session.exec(
        select(Image)
        .where(*filters)
        .order_by(Image.year.desc(), Image.team_number, Image.id)
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "counts": {s.value: counts.get(s, 0) for s in ImageStatus},
        "items": rows,
    }


@router.get("/images/{image_id}/file")
def image_file(image_id: int, session: Session = Depends(get_session)):
    image = session.get(Image, image_id)
    if image is None or not image.file_path or not Path(image.file_path).is_file():
        raise HTTPException(404, "Image not found")
    return FileResponse(image.file_path)
