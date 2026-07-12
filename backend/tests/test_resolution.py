"""Resolution tracking + best-available re-download remediation.

Covers the fix for legacy low-res VODs: ffprobe now reports height, the
reconciler backfills it, yt-dlp probes the best available height, the audit
requeues videos below that best available (codec-agnostic, so 1080p AV1 with
1080p available is left alone), and a re-download first removes the stale
on-disk file (at any extension / depth).
"""
import json
from pathlib import Path
from types import SimpleNamespace

from sqlmodel import Session, select


def test_ffprobe_reads_height(monkeypatch, tmp_path):
    from app.services import media_probe

    f = tmp_path / "clip.mp4"
    f.write_bytes(b"x" * 10)

    payload = {
        "format": {"format_name": "mov,mp4", "duration": "120.0"},
        "streams": [
            {"codec_type": "audio", "codec_name": "aac"},
            {"codec_type": "video", "codec_name": "av1",
             "width": 1920, "height": 1080},
        ],
    }
    monkeypatch.setattr(
        media_probe.subprocess, "run",
        lambda *a, **k: SimpleNamespace(stdout=json.dumps(payload)),
    )

    info = media_probe.ffprobe(f)
    assert info.height == 1080
    assert info.width == 1920
    assert info.vcodec == "av1"


def _make_video(session, youtube_id, path, height, orig_height=None):
    from app.models import Video, VideoStatus, utcnow
    v = Video(
        youtube_id=youtube_id, title="t", year=2026, event_key="2026test",
        status=VideoStatus.completed, present=True, file_path=str(path),
        orig_ext="mp4", orig_vcodec="h264", orig_size=100, orig_height=orig_height,
        current_ext="mp4", current_vcodec="h264", current_size=100,
        current_height=height, downloaded_at=utcnow(),
    )
    session.add(v)
    session.commit()
    session.refresh(v)
    return v


def test_reconcile_populates_and_backfills_height(temp_env, monkeypatch):
    from app.db import get_engine, set_setting
    from app.models import Video
    from app.services import reconciler
    from app.services.media_probe import MediaInfo

    media = Path(temp_env) / "media" / "2026" / "2026test"
    media.mkdir(parents=True, exist_ok=True)
    f = media / "Legacy VOD [CCCCCCCCCCC].mkv"
    f.write_bytes(b"z" * 50)

    with Session(get_engine()) as session:
        set_setting(session, "media_root", str(Path(temp_env) / "media"))
        session.commit()
        # Pre-existing row with no height recorded yet (predates tracking).
        _make_video(session, "CCCCCCCCCCC", f, height=None, orig_height=None)

    monkeypatch.setattr(
        reconciler, "ffprobe",
        lambda p: MediaInfo(container="matroska", vcodec="av1", size=50,
                            duration=120, width=640, height=360),
    )

    with Session(get_engine()) as session:
        reconciler.reconcile(session)
        v = session.exec(
            select(Video).where(Video.youtube_id == "CCCCCCCCCCC")
        ).first()
        assert v.current_height == 360
        assert v.orig_height == 360  # backfilled


def test_probe_reports_max_available_height(monkeypatch):
    from app.services import ytdlp_runner

    info = {
        "title": "t", "duration": 120, "live_status": None,
        "formats": [
            {"format_id": "18", "height": 360},
            {"format_id": "22", "height": 720},
            {"format_id": "137", "height": 1080},
            {"format_id": "251"},  # audio-only, no height
        ],
    }

    class FakeYDL:
        def __init__(self, opts):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download):
            return info

    monkeypatch.setattr(ytdlp_runner.yt_dlp, "YoutubeDL", FakeYDL)

    res = ytdlp_runner.probe("https://youtu.be/x", {})
    assert res.available_height == 1080


def test_resolution_audit_requeues_only_upgradable(temp_env, monkeypatch):
    from app.db import get_engine
    from app.models import DownloadJob, Video, VideoStatus
    from app.services import jobs
    from app.services.ytdlp_runner import ProbeResult

    with Session(get_engine()) as session:
        up = _make_video(session, "UPUPUPUPUPU", "/x/up.mp4", height=720)
        up.webpage_url = "https://youtu.be/up"
        best = _make_video(session, "BESTBESTBES", "/x/best.mp4", height=1080)
        best.webpage_url = "https://youtu.be/best"
        boom = _make_video(session, "BOOMBOOMBOO", "/x/boom.mp4", height=480)
        boom.webpage_url = "https://youtu.be/boom"
        session.commit()

    # Per-video probe results; the "boom" url raises to exercise error handling.
    available = {
        "https://youtu.be/up": 1080,     # 720 on disk < 1080 available -> requeue
        "https://youtu.be/best": 1080,   # 1080 == 1080 (e.g. AV1 transcode) -> keep
    }

    def fake_probe(url, settings):
        if url == "https://youtu.be/boom":
            raise RuntimeError("bot check")
        return ProbeResult(is_live=False, live_status=None, title="t",
                           duration=1, available_height=available[url])

    monkeypatch.setattr(jobs.ytdlp_runner, "probe", fake_probe)

    run = jobs.run_resolution_audit()
    assert run.discovered == 2  # up + best probed; boom raised
    assert run.enqueued == 1
    assert run.errors == 1

    with Session(get_engine()) as session:
        dljobs = session.exec(select(DownloadJob)).all()
        queued_ids = {session.get(Video, j.video_id).youtube_id for j in dljobs}
        assert queued_ids == {"UPUPUPUPUPU"}


def test_remove_existing_output_deletes_any_ext_any_depth(temp_env):
    from app.models import Video
    from app.services.worker import _remove_existing_output

    root = Path(temp_env) / "media"
    # Team-nested layout, different extension than a fresh download would use.
    nested = root / "frc2713" / "2026" / "2026necmp"
    nested.mkdir(parents=True, exist_ok=True)
    stale = nested / "Old 360p [DDDDDDDDDDD].mkv"
    stale.write_bytes(b"old")
    keep = nested / "Other Video [EEEEEEEEEEE].mp4"
    keep.write_bytes(b"keep")
    part = nested / "Old 360p [DDDDDDDDDDD].mp4.part"
    part.write_bytes(b"partial")

    video = Video(youtube_id="DDDDDDDDDDD", title="t")
    _remove_existing_output(str(root), video)

    assert not stale.exists()      # removed regardless of extension/depth
    assert keep.exists()           # unrelated id untouched
    assert part.exists()           # partials left to _cleanup_partials
