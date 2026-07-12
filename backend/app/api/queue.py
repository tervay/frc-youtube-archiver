"""Download queue endpoints: list jobs, retry, cancel, manual URL enqueue."""

from __future__ import annotations

import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, col, select

from ..models import DownloadJob, JobState, SourceType, Video, VideoStatus, utcnow
from ..services.queue import enqueue_video
from .deps import get_session

router = APIRouter(tags=["queue"])

_YT_ID = re.compile(r"(?:v=|youtu\.be/|/shorts/|/live/)([A-Za-z0-9_-]{11})")


class ManualUrl(BaseModel):
    url: str
    year: Optional[int] = None
    event_key: Optional[str] = None


def _extract_id(url: str) -> Optional[str]:
    m = _YT_ID.search(url)
    if m:
        return m.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", url.strip()):
        return url.strip()
    return None


@router.get("/queue")
def list_queue(session: Session = Depends(get_session), active_only: bool = False):
    stmt = select(DownloadJob, Video).join(
        Video, col(DownloadJob.video_id) == col(Video.id)
    )
    if active_only:
        # Ascending id puts running (claimed earliest) first, then pending in
        # the order they'll be downloaded — matches the dashboard's expectation.
        stmt = stmt.where(DownloadJob.state.in_([JobState.pending, JobState.running]))
        stmt = stmt.order_by(DownloadJob.id).limit(500)
    else:
        stmt = stmt.order_by(DownloadJob.created_at.desc()).limit(500)
    rows = session.exec(stmt).all()
    return [{"job": job, "video": video} for job, video in rows]


@router.post("/queue/retry-failed")
def retry_failed(session: Session = Depends(get_session)):
    """Requeue every video currently in the failed state."""
    failed = session.exec(select(Video).where(Video.status == VideoStatus.failed)).all()
    requeued = 0
    for video in failed:
        video.error = None
        session.add(video)
        session.commit()
        if enqueue_video(session, video):
            requeued += 1
    return {"requeued": requeued, "total_failed": len(failed)}


@router.post("/queue/{job_id}/retry")
def retry(job_id: int, session: Session = Depends(get_session)):
    job = session.get(DownloadJob, job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    if job.state in (JobState.pending, JobState.running):
        raise HTTPException(409, "Job is already active")
    job.state = JobState.pending
    job.started_at = None
    job.finished_at = None
    job.progress_pct = 0.0
    job.phase = "downloading"
    video = session.get(Video, job.video_id)
    if video:
        video.status = VideoStatus.queued
        session.add(video)
    session.add(job)
    session.commit()
    return {"retried": True}


@router.post("/queue/{job_id}/cancel")
def cancel(job_id: int, session: Session = Depends(get_session)):
    job = session.get(DownloadJob, job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    if job.state == JobState.running:
        raise HTTPException(409, "Cannot cancel a running download")
    if job.state == JobState.pending:
        job.state = JobState.canceled
        job.finished_at = utcnow()
        session.add(job)
        session.commit()
    return {"canceled": True}


@router.post("/queue/manual")
def manual_enqueue(payload: ManualUrl, session: Session = Depends(get_session)):
    vid = _extract_id(payload.url)
    if not vid:
        raise HTTPException(400, "Could not parse a YouTube video ID from that URL")

    video = session.exec(select(Video).where(Video.youtube_id == vid)).first()
    if video is None:
        video = Video(
            youtube_id=vid,
            title=f"Manual: {vid}",
            webpage_url=f"https://www.youtube.com/watch?v={vid}",
            source_type=SourceType.manual,
            year=payload.year,
            event_key=payload.event_key,
        )
        session.add(video)
        session.commit()
        session.refresh(video)
    else:
        video.force_redownload = False
        session.add(video)
        session.commit()

    job = enqueue_video(session, video)
    if job is None:
        raise HTTPException(409, "A download for this video is already active")
    return {"queued": True, "job_id": job.id, "video_id": video.id}
