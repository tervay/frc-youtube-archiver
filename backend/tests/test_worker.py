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

    # Event-type match video nests under year/event/matches.
    v = Video(
        youtube_id="EEEEEEEEEEE",
        title="t",
        source_type=SourceType.event_match,
        year=2026,
        event_key="2026necmp",
        match_key="2026necmp_qm1",
    )
    assert _dest_dir(root, v) == Path("/library/2026/2026necmp/matches")


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


def test_ensure_match_copies_makes_one_file_per_match(tmp_path):
    from app.models import SourceType, Video
    from app.services.worker import ensure_match_copies

    dest = tmp_path / "2012" / "2012arc" / "matches"
    dest.mkdir(parents=True)
    source = dest / "2012arc_qm1 [UcZyRY9f8x8].mkv"
    source.write_bytes(b"video")

    v = Video(
        youtube_id="UcZyRY9f8x8",
        title="t",
        source_type=SourceType.event_match,
        year=2012,
        event_key="2012arc",
        match_key="2012arc_qm1",
        match_keys="2012arc_qm1,2012arc_qm2,2012arc_qm3",
        file_path=str(source),
    )

    assert ensure_match_copies(str(tmp_path), v) == 2
    assert (dest / "2012arc_qm2 [UcZyRY9f8x8].mkv").read_bytes() == b"video"
    assert (dest / "2012arc_qm3 [UcZyRY9f8x8].mkv").read_bytes() == b"video"
    assert not list(dest.glob("*.copytmp"))

    # Idempotent, and a copy tdarr already re-encoded to another extension is
    # matched on the match key + id rather than the extension.
    assert ensure_match_copies(str(tmp_path), v) == 0
    (dest / "2012arc_qm3 [UcZyRY9f8x8].mkv").unlink()
    (dest / "2012arc_qm3 [UcZyRY9f8x8].mp4").write_bytes(b"transcoded")
    assert ensure_match_copies(str(tmp_path), v) == 0


def test_ensure_match_copies_skips_other_source_types(tmp_path):
    from app.models import SourceType, Video
    from app.services.worker import ensure_match_copies

    dest = tmp_path / "frc254" / "2026" / "2026necmp"
    dest.mkdir(parents=True)
    source = dest / "Quals 1 [AAAAAAAAAAA].mkv"
    source.write_bytes(b"video")

    v = Video(
        youtube_id="AAAAAAAAAAA",
        title="t",
        source_type=SourceType.match,
        team_keys="frc254",
        year=2026,
        event_key="2026necmp",
        match_key="2026necmp_qm1",
        match_keys="2026necmp_qm1,2026necmp_qm2",
        file_path=str(source),
    )

    assert ensure_match_copies(str(tmp_path), v) == 0
    assert len(list(dest.iterdir())) == 1


def test_ensure_team_copies_skips_first_team_for_match_source(tmp_path):
    from app.models import SourceType, Video
    from app.services.worker import ensure_team_copies

    dest = tmp_path / "frc254" / "2026" / "2026necmp"
    dest.mkdir(parents=True)
    source = dest / "Quals 1 [AAAAAAAAAAA].mkv"
    source.write_bytes(b"video")

    v = Video(
        youtube_id="AAAAAAAAAAA",
        title="t",
        source_type=SourceType.match,
        team_keys="frc254,frc118",
        year=2026,
        event_key="2026necmp",
        file_path=str(source),
    )

    # frc254 (the discovering team) already owns the file at _dest_dir; only
    # frc118 (a later team association) needs a copy.
    assert ensure_team_copies(str(tmp_path), v) == 1
    copy = tmp_path / "frc118" / "2026" / "2026necmp" / "Quals 1 [AAAAAAAAAAA].mkv"
    assert copy.read_bytes() == b"video"
    assert not list(copy.parent.glob("*.copytmp"))

    # Idempotent.
    assert ensure_team_copies(str(tmp_path), v) == 0


def test_ensure_team_copies_all_teams_for_event_match_source(tmp_path):
    from app.models import SourceType, Video
    from app.services.worker import ensure_team_copies

    dest = tmp_path / "2018" / "2018nyro" / "matches"
    dest.mkdir(parents=True)
    source = dest / "2018nyro_qm1 [AAAAAAAAAAA].mkv"
    source.write_bytes(b"video")

    v = Video(
        youtube_id="AAAAAAAAAAA",
        title="t",
        source_type=SourceType.event_match,
        team_keys="frc340,frc341",
        year=2018,
        event_key="2018nyro",
        match_key="2018nyro_qm1",
        file_path=str(source),
    )

    # A season/district-discovered video isn't nested under any team's folder,
    # so every associated team gets its own copy.
    assert ensure_team_copies(str(tmp_path), v) == 2
    for team in ("frc340", "frc341"):
        copy = tmp_path / team / "2018" / "2018nyro" / "2018nyro_qm1 [AAAAAAAAAAA].mkv"
        assert copy.read_bytes() == b"video"


def test_ensure_team_copies_noop_without_file_or_teams(tmp_path):
    from app.models import SourceType, Video
    from app.services.worker import ensure_team_copies

    v = Video(
        youtube_id="AAAAAAAAAAA",
        title="t",
        source_type=SourceType.event_match,
        team_keys="",
        year=2018,
        event_key="2018nyro",
    )
    assert ensure_team_copies(str(tmp_path), v) == 0

    v.team_keys = "frc340"
    v.file_path = str(tmp_path / "does_not_exist.mkv")
    assert ensure_team_copies(str(tmp_path), v) == 0


def _make_job(session, **video_kwargs):
    from app.models import DownloadJob, JobState, Video

    v = Video(youtube_id="AAAAAAAAAAA", title="t", **video_kwargs)
    session.add(v)
    session.commit()
    session.refresh(v)
    job = DownloadJob(video_id=v.id, state=JobState.pending)
    session.add(job)
    session.commit()
    session.refresh(job)
    return job.id


def test_run_job_treats_upcoming_probe_error_as_skipped_live(temp_env, monkeypatch):
    from app.db import get_engine
    from app.models import DownloadJob, JobState, Video, VideoStatus
    from app.services import worker, ytdlp_runner

    with Session(get_engine()) as session:
        job_id = _make_job(session)

    def fake_probe(url, settings):
        raise Exception(
            "ERROR: [youtube] AAAAAAAAAAA: This live event will begin in a few moments."
        )

    monkeypatch.setattr(ytdlp_runner, "probe", fake_probe)
    worker.run_job(job_id)

    with Session(get_engine()) as session:
        job = session.get(DownloadJob, job_id)
        video = session.get(Video, job.video_id)
        assert video.status == VideoStatus.skipped_live
        assert job.state == JobState.canceled
        assert job.attempts == 1  # never retried


def test_run_job_treats_unavailable_probe_error_as_terminal(temp_env, monkeypatch):
    from app.db import get_engine
    from app.models import DownloadJob, JobState, Video, VideoStatus
    from app.services import worker, ytdlp_runner

    with Session(get_engine()) as session:
        job_id = _make_job(session)

    def fake_probe(url, settings):
        raise Exception("ERROR: [youtube] AAAAAAAAAAA: Private video")

    monkeypatch.setattr(ytdlp_runner, "probe", fake_probe)
    worker.run_job(job_id)

    with Session(get_engine()) as session:
        job = session.get(DownloadJob, job_id)
        video = session.get(Video, job.video_id)
        assert video.status == VideoStatus.unavailable
        assert job.state == JobState.error
        assert job.attempts == 1  # never retried
        assert "Private video" in video.error


def test_run_job_still_retries_unclassified_probe_error(temp_env, monkeypatch):
    from app.db import get_engine
    from app.models import DownloadJob, JobState, Video, VideoStatus
    from app.services import worker, ytdlp_runner

    with Session(get_engine()) as session:
        job_id = _make_job(session)

    def fake_probe(url, settings):
        raise Exception("ERROR: [youtube] AAAAAAAAAAA: HTTP Error 403: Forbidden")

    monkeypatch.setattr(ytdlp_runner, "probe", fake_probe)
    worker.run_job(job_id)

    with Session(get_engine()) as session:
        job = session.get(DownloadJob, job_id)
        video = session.get(Video, job.video_id)
        assert video.status == VideoStatus.queued  # requeued, not terminal
        assert job.state == JobState.pending


def test_classify_error():
    from app.services.ytdlp_runner import classify_error

    assert (
        classify_error(
            "ERROR: [youtube] xxx: This live event will begin in a few moments."
        )
        == "upcoming"
    )
    assert classify_error("ERROR: [youtube] xxx: Private video") == "unavailable"
    assert classify_error("ERROR: [youtube] xxx: Video unavailable") == "unavailable"
    assert (
        classify_error("ERROR: [youtube] xxx: This video is unavailable")
        == "unavailable"
    )
    assert (
        classify_error(
            "ERROR: unable to download video data: HTTP Error 403: Forbidden"
        )
        is None
    )
    assert classify_error("") is None
