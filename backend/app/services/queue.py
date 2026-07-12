"""Helpers for moving a Video into the download queue."""

from __future__ import annotations

from typing import Optional

from sqlmodel import Session, select

from .. import events
from ..models import DownloadJob, JobState, Video, VideoStatus, utcnow


def enqueue_video(session: Session, video: Video) -> Optional[DownloadJob]:
    """Queue a video for download if it isn't already active.

    Returns the created job, or None if a pending/running job already exists.
    """
    active = session.exec(
        select(DownloadJob).where(
            DownloadJob.video_id == video.id,
            DownloadJob.state.in_([JobState.pending, JobState.running]),
        )
    ).first()
    if active is not None:
        return None

    video.status = VideoStatus.queued
    video.updated_at = utcnow()
    session.add(video)

    job = DownloadJob(video_id=video.id, state=JobState.pending)
    session.add(job)
    session.commit()
    session.refresh(job)
    events.publish(
        "job_queued",
        {
            "video_id": video.id,
            "job_id": job.id,
            "title": video.title,
            "youtube_id": video.youtube_id,
        },
    )
    return job
