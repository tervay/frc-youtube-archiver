"""Reconciler tests: transcode detection and missing-file handling."""

from pathlib import Path

from sqlmodel import Session, select

from app.services.reconciler import extract_youtube_id


def test_extract_youtube_id():
    assert extract_youtube_id("Some Title [dQw4w9WgXcQ].mp4") == "dQw4w9WgXcQ"
    assert extract_youtube_id("Some Title [dQw4w9WgXcQ].mkv") == "dQw4w9WgXcQ"
    assert extract_youtube_id("no id here.mp4") is None


def _make_video(session, youtube_id, path, ext, vcodec, size):
    from app.models import Video, VideoStatus, utcnow

    v = Video(
        youtube_id=youtube_id,
        title="t",
        year=2026,
        event_key="2026test",
        status=VideoStatus.completed,
        present=True,
        file_path=str(path),
        orig_ext=ext,
        orig_vcodec=vcodec,
        orig_size=size,
        current_ext=ext,
        current_vcodec=vcodec,
        current_size=size,
        downloaded_at=utcnow(),
    )
    session.add(v)
    session.commit()
    session.refresh(v)
    return v


def test_transcode_detected_on_extension_change(temp_env, monkeypatch):
    from app.db import get_engine, set_setting
    from app.models import Video
    from app.services import reconciler
    from app.services.media_probe import MediaInfo

    media = Path(temp_env) / "media" / "2026" / "2026test"
    media.mkdir(parents=True, exist_ok=True)
    original = media / "Final Match [AAAAAAAAAAA].mp4"
    original.write_bytes(b"x" * 100)

    with Session(get_engine()) as session:
        set_setting(session, "media_root", str(Path(temp_env) / "media"))
        session.commit()
        _make_video(session, "AAAAAAAAAAA", original, "mp4", "h264", 100)

    # tdarr replaces the mp4/h264 with an mkv/av1.
    original.unlink()
    transcoded = media / "Final Match [AAAAAAAAAAA].mkv"
    transcoded.write_bytes(b"y" * 60)

    monkeypatch.setattr(
        reconciler,
        "ffprobe",
        lambda p: MediaInfo(container="matroska", vcodec="av1", size=60, duration=120),
    )

    with Session(get_engine()) as session:
        run = reconciler.reconcile(session)
        assert run.enqueued == 1  # one newly-transcoded file
        v = session.exec(select(Video).where(Video.youtube_id == "AAAAAAAAAAA")).first()
        assert v.present is True
        assert v.transcoded is True
        assert v.current_ext == "mkv"
        assert v.current_vcodec == "av1"


def test_missing_file_flagged_not_redownloaded(temp_env):
    from app.db import get_engine, set_setting
    from app.models import DownloadJob, Video

    media = Path(temp_env) / "media"
    with Session(get_engine()) as session:
        set_setting(session, "media_root", str(media))
        session.commit()
        ghost = media / "2026" / "2026test" / "Gone [BBBBBBBBBBB].mkv"
        _make_video(session, "BBBBBBBBBBB", ghost, "mkv", "av1", 10)

    from app.services import reconciler

    with Session(get_engine()) as session:
        run = reconciler.reconcile(session)
        assert run.errors == 1  # one missing
        v = session.exec(select(Video).where(Video.youtube_id == "BBBBBBBBBBB")).first()
        assert v.present is False
        # Missing files must not be auto-requeued.
        jobs = session.exec(select(DownloadJob)).all()
        assert jobs == []
