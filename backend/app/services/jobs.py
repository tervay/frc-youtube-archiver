"""Entry points for the scan and reconcile jobs, safe to run in a thread."""
from __future__ import annotations

import logging

from sqlmodel import Session, col, select

from .. import events
from ..db import get_all_settings, get_engine
from ..models import ScanRun, Video, VideoStatus, utcnow
from . import ytdlp_runner
from .queue import enqueue_video
from .reconciler import reconcile
from .scanner import Scanner

log = logging.getLogger("archiver.audit")


def run_scan() -> ScanRun:
    with Session(get_engine()) as session:
        run = Scanner(session).run()
    events.publish("scan_done", {"kind": "scan", "discovered": run.discovered,
                                 "enqueued": run.enqueued, "ok": run.ok,
                                 "message": run.message})
    return run


def run_reconcile() -> ScanRun:
    with Session(get_engine()) as session:
        run = reconcile(session)
    events.publish("scan_done", {"kind": "reconcile", "message": run.message})
    return run


def run_resolution_audit() -> ScanRun:
    """Probe every completed video and requeue any below its best available res.

    For each completed video, ask yt-dlp what the highest resolution YouTube
    offers (one extract-info call per video — slow, and can trip bot detection
    on a large library) and compare it to the on-disk height. Codec is ignored:
    ``current_height`` is the true resolution even after tdarr transcodes to AV1
    in place, so a 1080p AV1 file with 1080p available is left alone while a
    720p file with 1080p available is requeued. Probe failures are tolerated
    per-video. The fresh higher-res download replaces the old file via the
    worker's ``_remove_existing_output``.
    """
    with Session(get_engine()) as session:
        run = ScanRun(kind="resolution_audit")
        session.add(run)
        session.commit()
        session.refresh(run)

        settings = get_all_settings(session)
        videos = session.exec(
            select(Video).where(
                Video.status == VideoStatus.completed,
                col(Video.current_height).is_not(None),
            )
        ).all()

        probed = requeued = errors = 0
        for video in videos:
            try:
                res = ytdlp_runner.probe(video.webpage_url, settings)
            except Exception as exc:  # noqa: BLE001 - one bad video mustn't halt the audit
                errors += 1
                log.warning("audit probe failed for %s: %s",
                            video.youtube_id, exc)
                continue
            probed += 1
            if (res.available_height
                    and res.available_height > video.current_height):
                video.error = None
                session.add(video)
                session.commit()
                if enqueue_video(session, video):
                    requeued += 1
                    log.info("audit requeued %s: on-disk %sp < available %sp",
                             video.youtube_id, video.current_height,
                             res.available_height)

        run.discovered = probed
        run.enqueued = requeued
        run.errors = errors
        run.finished_at = utcnow()
        run.message = (f"Requeued {requeued} of {probed} probed"
                       + (f"; {errors} probe errors" if errors else ""))
        session.add(run)
        session.commit()
        session.refresh(run)

    events.publish("scan_done", {"kind": "resolution_audit",
                                 "discovered": run.discovered,
                                 "enqueued": run.enqueued,
                                 "message": run.message})
    return run
