"""Dashboard summary stats."""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends
from sqlmodel import Session, func, select

from ..models import DownloadJob, JobState, Video, VideoStatus, utcnow
from .. import scheduler
from .deps import get_session

router = APIRouter(tags=["stats"])


def _count(session: Session, model, *where) -> int:
    stmt = select(func.count()).select_from(model)
    for w in where:
        stmt = stmt.where(w)
    return session.exec(stmt).one()


@router.get("/stats")
def stats(session: Session = Depends(get_session)):
    since = utcnow() - timedelta(hours=24)
    by_status = {
        s.value: _count(session, Video, Video.status == s) for s in VideoStatus
    }
    total_size = session.exec(
        select(func.coalesce(func.sum(Video.current_size), 0)).where(
            Video.present == True
        )  # noqa: E712
    ).one()
    return {
        "videos_total": _count(session, Video),
        "total_size": int(total_size or 0),
        "by_status": by_status,
        "completed_24h": _count(
            session,
            Video,
            Video.status == VideoStatus.completed,
            Video.downloaded_at >= since,
        ),
        "transcoded": _count(session, Video, Video.transcoded == True),  # noqa: E712
        "missing": _count(
            session,
            Video,
            Video.present == False,  # noqa: E712
            Video.file_path.is_not(None),
        ),
        "queue": {
            "pending": _count(
                session, DownloadJob, DownloadJob.state == JobState.pending
            ),
            "running": _count(
                session, DownloadJob, DownloadJob.state == JobState.running
            ),
            "error": _count(session, DownloadJob, DownloadJob.state == JobState.error),
        },
        "next_run": scheduler.next_run_times(),
    }
