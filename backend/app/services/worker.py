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
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

from sqlmodel import Session, col, select

from .. import events
from ..db import get_all_settings, get_engine
from ..models import DownloadJob, JobState, SourceType, Video, VideoStatus, utcnow
from . import ytdlp_runner
from .media_probe import ffprobe
from .reconciler import VIDEO_EXTS, extract_youtube_id

log = logging.getLogger("archiver.worker")

_MAX_LOG = 4000
_SPEED_WINDOW = 3.0  # seconds of samples to average speed/ETA over
_PUBLISH_EVERY = 0.5  # min seconds between SSE progress events
_DB_EVERY = 1.0  # min seconds between DB progress writes


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
            job.phase = "downloading"
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
        # Sized well above any sane ``concurrent_downloads`` so post-processing
        # (merge/mux) jobs — which no longer count against the download limit —
        # have headroom to run alongside fresh downloads. ``_dispatch_loop``
        # still caps total in-flight at this ceiling.
        self._executor = ThreadPoolExecutor(max_workers=16, thread_name_prefix="dl")
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
                    # A job in the post-processing (merge/mux) phase no longer
                    # occupies a download slot — only actively-downloading jobs
                    # count against ``concurrency``. Cap total in-flight at the
                    # pool size so piled-up merges can't exhaust the executor.
                    active_downloads = self._count_active_downloads(session)
                    free = max(0, concurrency - active_downloads)
                    free = min(free, self._executor._max_workers - len(self._futures))
                    if free > 0:
                        for job in self._claim(session, free):
                            active_downloads += 1
                            log.info(
                                "Dispatching job %d (video_id=%s) to worker "
                                "thread [%d/%d download slots in use]",
                                job.id,
                                job.video_id,
                                active_downloads,
                                concurrency,
                            )
                            fut = loop.run_in_executor(self._executor, run_job, job.id)
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

    def _count_active_downloads(self, session: Session) -> int:
        """In-flight jobs still pulling bytes (phase != postprocessing).

        Merging jobs are excluded so they don't hold a ``concurrent_downloads``
        slot. NULL phase (pre-migration rows) is treated as downloading.
        """
        if not self._futures:
            return 0
        rows = session.exec(
            select(DownloadJob.phase).where(
                col(DownloadJob.id).in_(list(self._futures))
            )
        ).all()
        return sum(1 for phase in rows if phase != "postprocessing")

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
            log.error(
                "run_job(%d): video_id=%s not found; marking job error",
                job_id,
                job.video_id,
            )
            job.state = JobState.error
            session.add(job)
            session.commit()
            return

        settings = get_all_settings(session)
        job.attempts += 1
        job.phase = "downloading"
        video.status = VideoStatus.downloading
        session.add_all([job, video])
        session.commit()
        log.info(
            "START job=%d video_id=%d attempt=%d yt=%s src=%s title=%r url=%s",
            job.id,
            video.id,
            job.attempts,
            video.youtube_id,
            video.source_type,
            video.title,
            video.webpage_url,
        )
        events.publish(
            "job_started",
            {"job_id": job.id, "video_id": video.id, "title": video.title},
        )

        t0 = time.monotonic()
        try:
            log.debug("job=%d probing live status", job.id)
            probe = ytdlp_runner.probe(video.webpage_url, settings)
            log.info(
                "job=%d probe: live=%s live_status=%s title=%r duration=%s",
                job.id,
                probe.is_live,
                probe.live_status,
                probe.title,
                probe.duration,
            )
            if probe.is_live:
                log.info(
                    "job=%d skipping: video is live (%s)", job.id, probe.live_status
                )
                _mark_skipped_live(session, job, video, probe.live_status)
                return
            if probe.title:
                video.title = probe.title

            dest = _dest_dir(settings["media_root"], video)
            # A re-download (force_redownload / skipped_live re-run) must replace
            # any file already on disk for this id: yt-dlp skips when the final
            # file exists, and the new file may land at a different extension
            # (tdarr AV1 .mkv vs a fresh .mp4), which would otherwise orphan the
            # old one. youtube_id dedup means run_job only reaches here for a new
            # or intentionally-requeued id, so removing an existing file is safe.
            _remove_existing_output(settings["media_root"], video)
            log.info(
                "job=%d downloading to %s (format=%r client=%r)",
                job.id,
                dest,
                settings.get("format_selector"),
                settings.get("youtube_player_client"),
            )
            # Shared between the download hook (which learns the combined size
            # and final path), the pp hook (which starts the merge monitor), and
            # the monitor thread itself.
            ctx: dict = {
                "combined_total": 0,
                "final_path": None,
                "dest_dir": dest,
                "stop": threading.Event(),
                "thread": None,
            }
            hook = _make_hook(job_id, video.id, ctx)
            pp_hook = _make_pp_hook(job_id, video.id, ctx)
            try:
                result = ytdlp_runner.download(
                    video.webpage_url, dest, settings, hook, pp_hook
                )
            finally:
                # Authoritative merge-monitor cleanup: pp "finished" may never
                # fire if ffmpeg raises, so always stop and join here.
                ctx["stop"].set()
                if ctx["thread"] is not None:
                    ctx["thread"].join(timeout=3)
            elapsed = time.monotonic() - t0
            log.info(
                "job=%d download OK in %.1fs -> %s (ext=%s vcodec=%s)",
                job.id,
                elapsed,
                result.get("filepath"),
                result.get("ext"),
                result.get("vcodec"),
            )
            _record_success(session, job, video, result)
        except Exception as e:  # noqa: BLE001
            elapsed = time.monotonic() - t0
            log.exception(
                "job=%d FAILED after %.1fs (video_id=%d url=%s): %s",
                job.id,
                elapsed,
                video.id,
                video.webpage_url,
                e,
            )
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


def _make_hook(job_id: int, video_id: int, ctx: Optional[dict] = None):
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
            stream_total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0

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

            # Hand the merge monitor the true combined size and the final merged
            # path (info["_filename"] is the post-merge target, e.g. .mkv).
            if ctx is not None:
                if combined:
                    ctx["combined_total"] = combined
                # Declared filesize/filesize_approx is frequently missing or a
                # low estimate for was_live/post-live streams, which otherwise
                # makes the merge progress bar peg near 100% almost instantly.
                # Track the actual peak downloaded bytes as a more reliable
                # stand-in for the remuxed output's final size.
                ctx["merge_total"] = max(ctx.get("merge_total", 0), cum_done)
                ctx["final_path"] = info.get("_filename") or filename

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
            eta = (
                ((combined - cum_done) / speed_bps)
                if (speed_bps and combined)
                else None
            )

            payload = {
                "job_id": job_id,
                "video_id": video_id,
                "progress_pct": round(min(pct, 100.0), 1),
                "speed": _fmt_speed(speed_bps),
                "eta": _fmt_eta(eta),
                "downloaded_bytes": cum_done,
                "total_bytes": combined,
            }
            if now - last_pub["t"] >= _PUBLISH_EVERY:
                last_pub["t"] = now
                events.publish("progress", payload)
            if now - last_db["t"] >= _DB_EVERY:
                last_db["t"] = now
                _persist_progress(job_id, payload)
        elif status == "finished":
            events.publish(
                "progress",
                {
                    "job_id": job_id,
                    "video_id": video_id,
                    "progress_pct": 100.0,
                    "speed": None,
                    "eta": None,
                },
            )

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


# Post-processors that merge the separate video+audio streams into one growing
# output file — the ones whose progress we can estimate from that file's size.
_MERGE_PPS = {"Merger", "FFmpegMerger"}


def _merge_temp_path(ctx: dict, youtube_id: str) -> Optional[Path]:
    """Locate yt-dlp's growing merge temp file (``<name>.temp.<ext>``).

    Prefers deriving it exactly from the final merged path; falls back to a
    directory scan keyed on the video id when that path isn't known yet.
    """
    final = ctx.get("final_path")
    if final:
        p = Path(final)
        return p.with_name(p.stem + ".temp" + p.suffix)
    # Fallback: scan the destination dir for the growing merge temp file.
    dest = ctx.get("dest_dir")
    if dest and youtube_id:
        try:
            for path in Path(dest).iterdir():
                name = path.name
                if (
                    youtube_id in name
                    and ".temp." in name
                    and path.suffix.lower() in VIDEO_EXTS
                ):
                    return path
        except OSError:
            pass
    return None


def _monitor_merge(
    job_id: int, video_id: int, label: str, youtube_id: str, ctx: dict
) -> None:
    """Poll the merge temp file's size and publish an approximate percentage.

    Runs in its own daemon thread (yt-dlp blocks the worker thread in ffmpeg
    during the merge). Exits when ``ctx['stop']`` is set by the pp "finished"
    event or run_job's finally block.
    """
    stop: threading.Event = ctx["stop"]
    while not stop.wait(2.0):
        total = ctx.get("merge_total") or ctx.get("combined_total") or 0
        temp = _merge_temp_path(ctx, youtube_id)
        pct: Optional[float] = None
        if temp is not None and total > 0:
            try:
                size = temp.stat().st_size
            except OSError:
                size = 0
            if size > 0:
                pct = min(size / total * 100.0, 99.0)
        payload = {
            "job_id": job_id,
            "video_id": video_id,
            "progress_pct": round(pct, 1) if pct is not None else 100.0,
            "speed": label,
            "eta": None,
            "phase": "postprocessing",
        }
        events.publish("progress", payload)
        _persist_progress(job_id, payload)


def _make_pp_hook(job_id: int, video_id: int, ctx: Optional[dict] = None):
    """Surface the merge/mux phase, which emits no download progress at all.

    For a real stream merge we also spawn a monitor thread that turns the
    growing output file into a moving progress bar; other post-processors keep
    the static badge.
    """
    last = {"t": 0.0}

    def hook(d: dict) -> None:
        status = d.get("status")
        pp = d.get("postprocessor", "")
        if status == "finished":
            if ctx is not None and pp in _MERGE_PPS:
                ctx["stop"].set()
            return
        if status not in ("started", "processing"):
            return
        label = _PP_LABELS.get(pp, "processing…")
        # Merging no longer holds a download slot — flip phase promptly (an
        # unthrottled write) so the dispatcher frees the slot within one tick.
        if pp in _MERGE_PPS:
            _persist_phase(job_id, "postprocessing")
            if ctx is not None and status == "started" and ctx.get("thread") is None:
                video = d.get("info_dict") or {}
                youtube_id = video.get("id") or ""
                t = threading.Thread(
                    target=_monitor_merge,
                    args=(job_id, video_id, label, youtube_id, ctx),
                    name=f"merge-{job_id}",
                    daemon=True,
                )
                ctx["thread"] = t
                t.start()
        payload = {
            "job_id": job_id,
            "video_id": video_id,
            "progress_pct": 100.0,  # streams are downloaded; now post-processing
            "speed": label,
            "eta": None,
            "phase": "postprocessing",
        }
        events.publish("progress", payload)
        now = time.monotonic()
        if now - last["t"] >= _DB_EVERY:
            last["t"] = now
            _persist_progress(job_id, payload)

    return hook


def _persist_phase(job_id: int, phase: str) -> None:
    """Immediately record a job's phase (unthrottled, small write)."""
    with Session(get_engine()) as session:
        job = session.get(DownloadJob, job_id)
        if job is None or job.state != JobState.running or job.phase == phase:
            return
        job.phase = phase
        session.add(job)
        session.commit()


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
        if p.get("phase"):
            job.phase = p["phase"]
        session.add(job)
        session.commit()


def _record_success(
    session: Session, job: DownloadJob, video: Video, result: dict
) -> None:
    filepath = result.get("filepath")
    media = ffprobe(filepath) if filepath else None
    if media:
        log.info(
            "job=%d ffprobe: container=%s vcodec=%s size=%s duration=%s",
            job.id,
            media.container,
            media.vcodec,
            media.size,
            media.duration,
        )
    else:
        log.warning("job=%d ffprobe produced no metadata for %s", job.id, filepath)

    video.status = VideoStatus.completed
    video.file_path = filepath
    video.orig_ext = result.get("ext")
    video.orig_container = media.container if media else None
    video.orig_vcodec = (media.vcodec if media else None) or result.get("vcodec")
    video.orig_size = media.size if media else None
    video.orig_height = media.height if media else None
    video.duration = (media.duration if media else None) or result.get("duration")
    video.downloaded_at = utcnow()
    video.present = True
    video.current_ext = video.orig_ext
    video.current_vcodec = video.orig_vcodec
    video.current_size = video.orig_size
    video.current_height = video.orig_height
    video.transcoded = False
    video.last_seen_at = utcnow()
    video.error = None
    video.updated_at = utcnow()

    job.state = JobState.done
    job.progress_pct = 100.0
    job.finished_at = utcnow()
    session.add_all([video, job])
    session.commit()
    log.info("DONE job=%d video_id=%d -> completed (%s)", job.id, video.id, filepath)
    events.publish(
        "job_done",
        {
            "job_id": job.id,
            "video_id": video.id,
            "title": video.title,
            "file_path": filepath,
        },
    )


def _mark_skipped_live(
    session: Session, job: DownloadJob, video: Video, live_status: Optional[str]
) -> None:
    video.status = VideoStatus.skipped_live
    video.updated_at = utcnow()
    job.state = JobState.canceled
    job.finished_at = utcnow()
    job.log_tail = f"skipped: live_status={live_status}"
    session.add_all([video, job])
    session.commit()
    events.publish(
        "job_skipped",
        {
            "job_id": job.id,
            "video_id": video.id,
            "reason": "live",
            "live_status": live_status,
        },
    )


def _remove_existing_output(media_root: str, video: Video) -> None:
    """Delete any completed on-disk file for this video before a re-download.

    Walks the media root recursively (layout-agnostic, like the reconciler) and
    removes files whose embedded ``[VIDEOID]`` matches — regardless of extension
    or directory — so the fresh download replaces the old one instead of being
    skipped or orphaned. Only final media files are matched (the ``[id]`` token
    sits right before the extension), so ``.part``/``.ytdl`` artifacts are left
    to ``_cleanup_partials``.
    """
    root = Path(media_root)
    if not root.exists():
        return
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in VIDEO_EXTS:
            continue
        if extract_youtube_id(path.name) != video.youtube_id:
            continue
        try:
            path.unlink()
            log.info(
                "removed existing file %s before re-download of %s",
                path,
                video.youtube_id,
            )
        except OSError as exc:
            log.warning("could not remove existing file %s: %s", path, exc)


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


def _handle_failure(
    session: Session, job: DownloadJob, video: Video, settings: dict, error: str
) -> None:
    job.log_tail = (job.log_tail + "\n" + error)[-_MAX_LOG:]
    video.retry_count += 1
    max_retries = int(settings["max_retries"])
    if job.attempts <= max_retries:
        # Requeue for another pass. Drop any partial first so the next attempt
        # re-fetches fresh format URLs instead of resuming a stale .part.
        _cleanup_partials(settings["media_root"], video)
        log.warning(
            "job=%d will RETRY (attempt %d/%d) video_id=%d: %s",
            job.id,
            job.attempts,
            max_retries,
            video.id,
            error,
        )
        job.state = JobState.pending
        job.started_at = None
        job.phase = "downloading"
        video.status = VideoStatus.queued
    else:
        log.error(
            "job=%d GAVE UP after %d attempts (max=%d) video_id=%d: %s",
            job.id,
            job.attempts,
            max_retries,
            video.id,
            error,
        )
        job.state = JobState.error
        job.finished_at = utcnow()
        video.status = VideoStatus.failed
        video.error = error
    session.add_all([job, video])
    session.commit()
    events.publish(
        "job_error",
        {
            "job_id": job.id,
            "video_id": video.id,
            "error": error,
            "will_retry": job.state == JobState.pending,
        },
    )
