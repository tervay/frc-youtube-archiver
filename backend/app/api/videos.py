"""History / video-catalog endpoints."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlmodel import Session, func, or_, select

from ..models import Video, VideoStatus
from ..services.queue import enqueue_video
from .deps import get_session

router = APIRouter(tags=["videos"])


@router.get("/videos")
def list_videos(
    session: Session = Depends(get_session),
    q: Optional[str] = None,
    year: Optional[int] = None,
    status: Optional[VideoStatus] = None,
    event_key: Optional[str] = None,
    source_type: Optional[str] = None,
    transcoded: Optional[bool] = None,
    present: Optional[bool] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
):
    stmt = select(Video)
    count_stmt = select(func.count()).select_from(Video)

    filters = []
    if q:
        like = f"%{q}%"
        filters.append(or_(Video.title.ilike(like), Video.event_key.ilike(like),
                           Video.team_keys.ilike(like),
                           Video.youtube_id.ilike(like)))
    if year is not None:
        filters.append(Video.year == year)
    if status is not None:
        filters.append(Video.status == status)
    if event_key:
        filters.append(Video.event_key == event_key)
    if source_type:
        filters.append(Video.source_type == source_type)
    if transcoded is not None:
        filters.append(Video.transcoded == transcoded)
    if present is not None:
        filters.append(Video.present == present)

    for f in filters:
        stmt = stmt.where(f)
        count_stmt = count_stmt.where(f)

    total = session.exec(count_stmt).one()
    rows = session.exec(
        stmt.order_by(Video.updated_at.desc())
        .offset((page - 1) * page_size).limit(page_size)
    ).all()
    return {"total": total, "page": page, "page_size": page_size, "items": rows}


@router.get("/videos/{video_id}")
def get_video(video_id: int, session: Session = Depends(get_session)):
    video = session.get(Video, video_id)
    if video is None:
        raise HTTPException(404, "Video not found")
    return video


@router.post("/videos/{video_id}/redownload")
def redownload(video_id: int, session: Session = Depends(get_session)):
    """Force a re-download even though the video was already fetched."""
    video = session.get(Video, video_id)
    if video is None:
        raise HTTPException(404, "Video not found")
    video.force_redownload = False
    session.add(video)
    session.commit()
    job = enqueue_video(session, video)
    if job is None:
        raise HTTPException(409, "A download for this video is already active")
    return {"queued": True, "job_id": job.id}
