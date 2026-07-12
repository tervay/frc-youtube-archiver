"""Tests for worker helpers: startup recovery and speed/ETA formatting."""

from sqlmodel import Session, select


def test_recover_interrupted_jobs_requeues_running(temp_env):
    from app.db import get_engine
    from app.models import DownloadJob, JobState, Video, VideoStatus
    from app.services.worker import recover_interrupted_jobs

    with Session(get_engine()) as session:
        v = Video(youtube_id="AAAAAAAAAAA", title="t", status=VideoStatus.downloading)
        session.add(v)
        session.commit()
        session.refresh(v)
        # A job the previous process was mid-download on when it died.
        session.add(
            DownloadJob(video_id=v.id, state=JobState.running, progress_pct=42.0)
        )
        # A finished job that must be left untouched.
        session.add(DownloadJob(video_id=v.id, state=JobState.done))
        session.commit()

    n = recover_interrupted_jobs()
    assert n == 1

    with Session(get_engine()) as session:
        states = sorted(j.state for j in session.exec(select(DownloadJob)).all())
        assert JobState.pending in states  # running -> pending
        assert JobState.done in states  # done left alone
        v = session.exec(select(Video).where(Video.youtube_id == "AAAAAAAAAAA")).first()
        assert v.status == VideoStatus.queued


def test_combined_total_sums_streams():
    from app.services.worker import _combined_total

    # Video (11.3 GB) + audio (357 MB) should sum, not show only the audio.
    info = {
        "requested_formats": [
            {"filesize": 11_346_617_729},
            {"filesize": 374_319_088},
        ]
    }
    assert _combined_total(info) == 11_720_936_817
    # Falls back to approx sizes and tolerates missing values.
    assert (
        _combined_total({"requested_formats": [{"filesize_approx": 1000}, {}]}) == 1000
    )
    assert _combined_total({}) == 0


def test_dest_dir_layout():
    from pathlib import Path

    from app.models import SourceType, Video
    from app.services.worker import _dest_dir

    root = "/library"

    # Team-source (match) video nests under the team key.
    v = Video(
        youtube_id="AAAAAAAAAAA",
        title="t",
        source_type=SourceType.match,
        team_keys="frc254",
        year=2026,
        event_key="2026necmp",
    )
    assert _dest_dir(root, v) == Path("/library/frc254/2026/2026necmp")

    # Shared match video uses the first (discovering) team key.
    v = Video(
        youtube_id="BBBBBBBBBBB",
        title="t",
        source_type=SourceType.match,
        team_keys="frc254,frc118",
        year=2026,
        event_key="2026necmp",
    )
    assert _dest_dir(root, v) == Path("/library/frc254/2026/2026necmp")

    # Season/district (event_vod) video keeps the flat year/event layout.
    v = Video(
        youtube_id="CCCCCCCCCCC",
        title="t",
        source_type=SourceType.event_vod,
        year=2026,
        event_key="2026necmp",
    )
    assert _dest_dir(root, v) == Path("/library/2026/2026necmp")

    # A match video with no team keys falls back to the flat layout.
    v = Video(
        youtube_id="DDDDDDDDDDD",
        title="t",
        source_type=SourceType.match,
        team_keys="",
        year=2026,
        event_key="2026necmp",
    )
    assert _dest_dir(root, v) == Path("/library/2026/2026necmp")


def test_speed_and_eta_formatting():
    from app.services.worker import _fmt_eta, _fmt_speed

    assert _fmt_speed(None) is None
    assert _fmt_speed(0) is None
    assert _fmt_speed(1536) == "1.5 KB/s"
    assert _fmt_speed(5 * 1024 * 1024) == "5.0 MB/s"

    assert _fmt_eta(None) is None
    assert _fmt_eta(-1) is None
    assert _fmt_eta(float("nan")) is None
    assert _fmt_eta(83) == "1:23"
    assert _fmt_eta(3725) == "1:02:05"
