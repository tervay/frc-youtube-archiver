"""Background download manager.

An asyncio dispatch loop claims ``pending`` jobs (up to the configured
concurrency) and runs each one in a thread pool. The thread runs yt-dlp, whose
progress hook writes throttled updates to the DB and streams every tick to the
dashboard over SSE. On success the file is ffprobe'd and the ``orig_*`` columns
are recorded so the reconciler can later detect tdarr's re-encode.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

from sqlmodel import Session, select

from .. import events
from ..db import get_all_settings, get_engine
from ..models import (DownloadJob, JobState, SourceType, Video, VideoStatus, utcnow)
from . import ytdlp_runner
from .media_probe import ffprobe

log = logging.getLogger("archiver.worker")

_MAX_LOG = 4000
_SPEED_WINDOW = 3.0   # seconds of samples to average speed/ETA over
_PUBLISH_EVERY = 0.5  # min seconds between SSE progress events
_DB_EVERY = 1.0       # min seconds between DB progress writes


def recover_interrupted_jobs() -> int:
    """Requeue jobs left 'running' by a crash/restart so they resume.

    The worker only ever claims ``pending`` jobs, so anything stuck in
    ``running`` after the process died would otherwise be orphaned forever.
    """
    with Session(get_engine()) as session:
        jobs = session.exec(
            select(DownloadJob).where(DownloadJob.state == JobState.running)
        ).all()
        for job in jobs:
            job.state = JobState.pending
            job.started_at = None
            job.progress_pct = 0.0
            video = session.get(Video, job.video_id)
            if video and video.status == VideoStatus.downloading:
                video.status = VideoStatus.queued
                session.add(video)
            session.add(job)
        session.commit()
        return len(jobs)


def _fmt_speed(bps: Optional[float]) -> Optional[str]:
    if not bps or bps <= 0:
        return None
    val = float(bps)
    for unit in ("B/s", "KB/s", "MB/s", "GB/s"):
        if val < 1024:
            return f"{val:.1f} {unit}"
        val /= 1024
    return f"{val:.1f} TB/s"


def _fmt_eta(secs: Optional[float]) -> Optional[str]:
    if secs is None or secs != secs or secs < 0:  # None / NaN / negative
        return None
    secs = int(secs)
    h, m, s = secs // 3600, (secs % 3600) // 60, secs % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _dest_dir(media_root: str, video: Video) -> Path:
    year = str(video.year or "unknown")
    event = video.event_key or "misc"
    base = Path(media_root)
    # Team-source (match) videos are grouped under the discovering team's key,
    # e.g. /library/frc2713/2026/2026necmp/. The first key in team_keys is the
    # team that discovered the video first; shared matches thus stay under that
    # team and are never re-downloaded for a later team source (youtube_id dedup).
    if video.source_type == SourceType.match:
        team = next((k for k in video.team_keys.split(",") if k), None)
        if team:
            base = base / team
    return base / year / event


class DownloadManager:
    def __init__(self) -> None:
        self._executor = ThreadPoolExecutor(max_workers=8,
                                            thread_name_prefix="dl")
        self._futures: dict[int, asyncio.Future] = {}
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()

    def start(self) -> None:
        self._task = asyncio.create_task(self._dispatch_loop())

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            await self._task
        self._executor.shutdown(wait=False, cancel_futures=True)

    def wake(self) -> None:
        """Nudge the dispatcher (e.g. right after enqueueing)."""
        # The loop polls every second; nothing needed, but kept for callers.

    async def _dispatch_loop(self) -> None:
        loop = asyncio.get_running_loop()
        while not self._stop.is_set():
            try:
                self._reap()
                with Session(get_engine()) as session:
                    concurrency = int(get_all_settings(session)["concurrent_downloads"])
                    free = max(0, concurrency - len(self._futures))
                    if free:
                        for job in self._claim(session, free):
                            log.info("Dispatching job %d (video_id=%s) to worker "
                                     "thread [%d/%d slots in use]", job.id,
                                     job.video_id, len(self._futures) + 1,
                                     concurrency)
                            fut = loop.run_in_executor(self._executor,
                                                       run_job, job.id)
                            self._futures[job.id] = fut
            except Exception:  # keep the loop alive no matter what
                log.exception("Dispatch loop iteration failed (continuing)")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                pass

    def _reap(self) -> None:
        for job_id, fut in list(self._futures.items()):
            if fut.done():
                self._futures.pop(job_id, None)

    def _claim(self, session: Session, limit: int) -> list[DownloadJob]:
        jobs = session.exec(
            select(DownloadJob)
            .where(DownloadJob.state == JobState.pending)
            .order_by(DownloadJob.id)
            .limit(limit)
        ).all()
        for job in jobs:
            job.state = JobState.running
            job.started_at = utcnow()
            session.add(job)
        session.commit()
        return jobs


def run_job(job_id: int) -> None:
    """Execute a single download job. Runs in a worker thread."""
    with Session(get_engine()) as session:
        job = session.get(DownloadJob, job_id)
        if job is None:
            log.warning("run_job(%d): job vanished before it could start", job_id)
            return
        video = session.get(Video, job.video_id)
        if video is None:
            log.error("run_job(%d): video_id=%s not found; marking job error",
                      job_id, job.video_id)
            job.state = JobState.error
            session.add(job)
            session.commit()
            return

        settings = get_all_settings(session)
        job.attempts += 1
        video.status = VideoStatus.downloading
        session.add_all([job, video])
        session.commit()
        log.info("START job=%d video_id=%d attempt=%d yt=%s src=%s title=%r url=%s",
                 job.id, video.id, job.attempts, video.youtube_id,
                 video.source_type, video.title, video.webpage_url)
        events.publish("job_started", {"job_id": job.id, "video_id": video.id,
                                       "title": video.title})

        t0 = time.monotonic()
        try:
            log.debug("job=%d probing live status", job.id)
            probe = ytdlp_runner.probe(video.webpage_url, settings)
            log.info("job=%d probe: live=%s live_status=%s title=%r duration=%s",
                     job.id, probe.is_live, probe.live_status, probe.title,
                     probe.duration)
            if probe.is_live:
                log.info("job=%d skipping: video is live (%s)", job.id,
                         probe.live_status)
                _mark_skipped_live(session, job, video, probe.live_status)
                return
            if probe.title:
                video.title = probe.title

            dest = _dest_dir(settings["media_root"], video)
            log.info("job=%d downloading to %s (format=%r client=%r)", job.id,
                     dest, settings.get("format_selector"),
                     settings.get("youtube_player_client"))
            hook = _make_hook(job_id, video.id)
            pp_hook = _make_pp_hook(job_id, video.id)
            result = ytdlp_runner.download(video.webpage_url, dest, settings,
                                           hook, pp_hook)
            elapsed = time.monotonic() - t0
            log.info("job=%d download OK in %.1fs -> %s (ext=%s vcodec=%s)",
                     job.id, elapsed, result.get("filepath"), result.get("ext"),
                     result.get("vcodec"))
            _record_success(session, job, video, result)
        except Exception as e:  # noqa: BLE001
            elapsed = time.monotonic() - t0
            log.exception("job=%d FAILED after %.1fs (video_id=%d url=%s): %s",
                          job.id, elapsed, video.id, video.webpage_url, e)
            _handle_failure(session, job, video, settings, str(e))


def _combined_total(info: dict) -> int:
    """Sum the sizes of all requested formats (video+audio) for the true total.

    A merged download fetches separate streams, and yt-dlp's per-tick
    ``total_bytes`` reflects only the *current* stream (often the small audio),
    which made the dashboard show e.g. 357 MB for an 11 GB video.
    """
    total = 0
    for f in info.get("requested_formats") or []:
        total += f.get("filesize") or f.get("filesize_approx") or 0
    return total


def _make_hook(job_id: int, video_id: int):
    # (monotonic_time, cumulative_bytes) samples for a rolling-average speed.
    samples: deque[tuple[float, int]] = deque()
    last_pub = {"t": 0.0}
    last_db = {"t": 0.0}
    # Track cumulative progress across the video+audio streams of one download.
    st = {"base": 0, "file": None, "stream_total": 0}

    def hook(d: dict) -> None:
        status = d.get("status")
        if status == "downloading":
            now = time.monotonic()
            info = d.get("info_dict") or {}
            filename = d.get("filename") or info.get("_filename")
            done = d.get("downloaded_bytes") or 0
            stream_total = (d.get("total_bytes")
                            or d.get("total_bytes_estimate") or 0)

            # A new stream started (new temp file) -> bank the previous one.
            if st["file"] is None:
                st["file"] = filename
            elif filename != st["file"]:
                st["base"] += st["stream_total"]
                st["file"] = filename
            st["stream_total"] = stream_total

            combined = _combined_total(info) or stream_total
            cum_done = st["base"] + done
            pct = (cum_done / combined * 100.0) if combined else 0.0

            # Rolling average over the last few seconds smooths out yt-dlp's
            # very noisy per-chunk speed/ETA readings.
            samples.append((now, cum_done))
            while len(samples) > 1 and now - samples[0][0] > _SPEED_WINDOW:
                samples.popleft()
            speed_bps: Optional[float] = None
            if len(samples) >= 2:
                dt = samples[-1][0] - samples[0][0]
                dbytes = samples[-1][1] - samples[0][1]
                if dt > 0:
                    speed_bps = dbytes / dt
            if speed_bps is None:
                speed_bps = d.get("speed")
            eta = ((combined - cum_done) / speed_bps) \
                if (speed_bps and combined) else None

            payload = {
                "job_id": job_id, "video_id": video_id,
                "progress_pct": round(min(pct, 100.0), 1),
                "speed": _fmt_speed(speed_bps),
                "eta": _fmt_eta(eta),
                "downloaded_bytes": cum_done, "total_bytes": combined,
            }
            if now - last_pub["t"] >= _PUBLISH_EVERY:
                last_pub["t"] = now
                events.publish("progress", payload)
            if now - last_db["t"] >= _DB_EVERY:
                last_db["t"] = now
                _persist_progress(job_id, payload)
        elif status == "finished":
            events.publish("progress", {"job_id": job_id, "video_id": video_id,
                                        "progress_pct": 100.0, "speed": None,
                                        "eta": None})

    return hook


# yt-dlp postprocessor class name -> friendly label shown on the dashboard.
_PP_LABELS = {
    "Merger": "merging…",
    "FFmpegMerger": "merging…",
    "FFmpegVideoRemuxer": "remuxing…",
    "FFmpegVideoConvertor": "converting…",
    "ExtractAudio": "extracting audio…",
    "FFmpegExtractAudio": "extracting audio…",
    "FFmpegMetadata": "writing metadata…",
    "MoveFiles": "finalizing…",
    "EmbedThumbnail": "processing…",
    "FFmpegFixupM3u8": "processing…",
}


def _make_pp_hook(job_id: int, video_id: int):
    """Surface the merge/mux phase, which emits no download progress at all."""
    last = {"t": 0.0}

    def hook(d: dict) -> None:
        if d.get("status") not in ("started", "processing"):
            return
        label = _PP_LABELS.get(d.get("postprocessor", ""), "processing…")
        payload = {
            "job_id": job_id, "video_id": video_id,
            "progress_pct": 100.0,  # streams are downloaded; now post-processing
            "speed": label, "eta": None, "phase": "processing",
        }
        events.publish("progress", payload)
        now = time.monotonic()
        if now - last["t"] >= _DB_EVERY:
            last["t"] = now
            _persist_progress(job_id, payload)

    return hook


def _persist_progress(job_id: int, p: dict) -> None:
    with Session(get_engine()) as session:
        job = session.get(DownloadJob, job_id)
        if job is None or job.state != JobState.running:
            return
        job.progress_pct = p["progress_pct"]
        job.speed = str(p.get("speed") or "")
        job.eta = str(p.get("eta") or "")
        job.downloaded_bytes = p.get("downloaded_bytes", job.downloaded_bytes)
        job.total_bytes = p.get("total_bytes", job.total_bytes)
        session.add(job)
        session.commit()


def _record_success(session: Session, job: DownloadJob, video: Video,
                    result: dict) -> None:
    filepath = result.get("filepath")
    media = ffprobe(filepath) if filepath else None
    if media:
        log.info("job=%d ffprobe: container=%s vcodec=%s size=%s duration=%s",
                 job.id, media.container, media.vcodec, media.size, media.duration)
    else:
        log.warning("job=%d ffprobe produced no metadata for %s", job.id, filepath)

    video.status = VideoStatus.completed
    video.file_path = filepath
    video.orig_ext = result.get("ext")
    video.orig_container = media.container if media else None
    video.orig_vcodec = (media.vcodec if media else None) or result.get("vcodec")
    video.orig_size = media.size if media else None
    video.duration = (media.duration if media else None) or result.get("duration")
    video.downloaded_at = utcnow()
    video.present = True
    video.current_ext = video.orig_ext
    video.current_vcodec = video.orig_vcodec
    video.current_size = video.orig_size
    video.transcoded = False
    video.last_seen_at = utcnow()
    video.error = None
    video.updated_at = utcnow()

    job.state = JobState.done
    job.progress_pct = 100.0
    job.finished_at = utcnow()
    session.add_all([video, job])
    session.commit()
    log.info("DONE job=%d video_id=%d -> completed (%s)", job.id, video.id,
             filepath)
    events.publish("job_done", {"job_id": job.id, "video_id": video.id,
                                "title": video.title, "file_path": filepath})


def _mark_skipped_live(session: Session, job: DownloadJob, video: Video,
                       live_status: Optional[str]) -> None:
    video.status = VideoStatus.skipped_live
    video.updated_at = utcnow()
    job.state = JobState.canceled
    job.finished_at = utcnow()
    job.log_tail = f"skipped: live_status={live_status}"
    session.add_all([video, job])
    session.commit()
    events.publish("job_skipped", {"job_id": job.id, "video_id": video.id,
                                   "reason": "live", "live_status": live_status})


def _cleanup_partials(media_root: str, video: Video) -> None:
    """Remove yt-dlp partial artifacts for this video so a retry starts fresh.

    On a hard failure (e.g. HTTP 403 from an expired format URL) the leftover
    ``.part`` would otherwise be *resumed* on the next attempt against a newly
    signed URL — which keeps 403ing. Only partial artifacts are touched; the
    completed output file (which has none of these suffixes) is never removed.
    """
    try:
        dest = _dest_dir(media_root, video)
        if not dest.is_dir():
            return
        for path in dest.iterdir():
            name = path.name
            if video.youtube_id not in name:
                continue
            if name.endswith((".part", ".ytdl")) or ".part-Frag" in name:
                try:
                    path.unlink()
                    log.info("removed stale partial %s for retry", path)
                except OSError as exc:
                    log.warning("could not remove partial %s: %s", path, exc)
    except Exception as exc:  # never let cleanup block a requeue
        log.warning("partial cleanup failed for video_id=%d: %s", video.id, exc)


def _handle_failure(session: Session, job: DownloadJob, video: Video,
                    settings: dict, error: str) -> None:
    job.log_tail = (job.log_tail + "\n" + error)[-_MAX_LOG:]
    video.retry_count += 1
    max_retries = int(settings["max_retries"])
    if job.attempts <= max_retries:
        # Requeue for another pass. Drop any partial first so the next attempt
        # re-fetches fresh format URLs instead of resuming a stale .part.
        _cleanup_partials(settings["media_root"], video)
        log.warning("job=%d will RETRY (attempt %d/%d) video_id=%d: %s",
                    job.id, job.attempts, max_retries, video.id, error)
        job.state = JobState.pending
        job.started_at = None
        video.status = VideoStatus.queued
    else:
        log.error("job=%d GAVE UP after %d attempts (max=%d) video_id=%d: %s",
                  job.id, job.attempts, max_retries, video.id, error)
        job.state = JobState.error
        job.finished_at = utcnow()
        video.status = VideoStatus.failed
        video.error = error
    session.add_all([job, video])
    session.commit()
    events.publish("job_error", {"job_id": job.id, "video_id": video.id,
                                 "error": error, "will_retry":
                                 job.state == JobState.pending})
