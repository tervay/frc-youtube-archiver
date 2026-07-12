"""Reconcile DB rows against what's actually on disk.

Walks the media root, pulls the embedded ``[VIDEOID]`` token out of each
filename (regardless of extension), and matches it back to a Video row. It then
refreshes the on-disk columns and flips ``transcoded`` when the current
container/codec/size differs from what we originally downloaded — which is
exactly what tdarr's AV1/MKV re-encode looks like, even when YouTube's source
was already AV1 (size will still change).

Rows whose file has vanished are marked ``present=False`` but never
auto-redownloaded; that's a manual, user-driven action.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from sqlmodel import Session, select

from ..db import get_setting
from ..models import ScanRun, Video, utcnow
from .media_probe import ffprobe

# YouTube IDs are 11 chars of [A-Za-z0-9_-]; match the "[id]" token yt-dlp adds.
ID_TOKEN = re.compile(r"\[([A-Za-z0-9_-]{11})\](?=\.[^.]+$)")

VIDEO_EXTS = {".mkv", ".mp4", ".webm", ".mov", ".m4v", ".avi", ".ts", ".flv"}


def extract_youtube_id(filename: str) -> Optional[str]:
    m = ID_TOKEN.search(filename)
    return m.group(1) if m else None


def reconcile(session: Session) -> ScanRun:
    run = ScanRun(kind="reconcile")
    media_root = Path(get_setting(session, "media_root"))

    # Map youtube_id -> file on disk.
    found: dict[str, Path] = {}
    if media_root.exists():
        for path in media_root.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in VIDEO_EXTS:
                continue
            vid = extract_youtube_id(path.name)
            if vid:
                found[vid] = path

    updated = 0
    transcoded_count = 0
    missing = 0

    videos = session.exec(select(Video)).all()
    for video in videos:
        path = found.get(video.youtube_id)
        if path is None:
            if video.file_path and video.present:
                video.present = False
                video.last_seen_at = utcnow()
                video.updated_at = utcnow()
                session.add(video)
                missing += 1
            continue

        info = ffprobe(path)
        video.present = True
        video.file_path = str(path)
        video.current_ext = path.suffix.lstrip(".")
        video.current_vcodec = info.vcodec
        video.current_size = info.size
        video.current_height = info.height
        # Backfill the originally-downloaded height for pre-existing rows that
        # predate height tracking; tdarr preserves resolution, so the on-disk
        # height is a safe stand-in for what we first fetched.
        if video.orig_height is None:
            video.orig_height = info.height
        video.last_seen_at = utcnow()

        changed = _differs(video, info)
        if changed and not video.transcoded:
            transcoded_count += 1
        video.transcoded = changed
        video.updated_at = utcnow()
        session.add(video)
        updated += 1

    run.discovered = updated
    run.enqueued = transcoded_count
    run.errors = missing
    run.message = (f"updated={updated} transcoded={transcoded_count} "
                   f"missing={missing}")
    run.finished_at = utcnow()
    session.add(run)
    session.commit()
    session.refresh(run)  # keep attributes loaded after the session closes
    return run


def _differs(video: Video, info) -> bool:
    """True if the on-disk file no longer matches the originally downloaded one."""
    if video.orig_vcodec and info.vcodec and info.vcodec != video.orig_vcodec:
        return True
    if video.orig_ext and video.current_ext and \
            video.current_ext.lower() != video.orig_ext.lower():
        return True
    if video.orig_size and info.size and info.size != video.orig_size:
        return True
    return False
