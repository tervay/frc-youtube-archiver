"""Data-migration tests: collapsing TBA's timestamp-suffixed video ids."""

from sqlmodel import Session, select


def _add(session, youtube_id, match_key, status, file_path=None, error=None):
    from app.models import DownloadJob, SourceType, Video, VideoStatus

    v = Video(
        youtube_id=youtube_id,
        title=match_key,
        source_type=SourceType.event_match,
        year=2012,
        event_key="2012arc",
        match_key=match_key,
        status=VideoStatus(status),
        file_path=file_path,
        error=error,
    )
    session.add(v)
    session.commit()
    session.refresh(v)
    session.add(DownloadJob(video_id=v.id))
    session.commit()
    return v


def test_suffixed_ids_merge_into_one_row(temp_env, tmp_path):
    from app.db import _merge_suffixed_youtube_ids, get_engine
    from app.models import DownloadJob, Video, VideoStatus

    kept_file = tmp_path / "2012arc_qm2 [UcZyRY9f8x8].mkv"
    kept_file.write_bytes(b"video")

    with Session(get_engine()) as session:
        _add(session, "UcZyRY9f8x8?t=171", "2012arc_qm1", "queued")
        _add(session, "UcZyRY9f8x8", "2012arc_qm2", "completed", str(kept_file))
        _add(session, "UcZyRY9f8x8?t=356", "2012arc_qm3", "failed")
        _add(session, "AAAAAAAAAAA", "2012arc_qm4", "completed")

    _merge_suffixed_youtube_ids()

    with Session(get_engine()) as session:
        vids = session.exec(select(Video)).all()
        assert len(vids) == 2
        merged = session.exec(
            select(Video).where(Video.youtube_id == "UcZyRY9f8x8")
        ).first()
        # The completed row with a file on disk is the one kept.
        assert merged.status == VideoStatus.completed
        assert merged.match_key == "2012arc_qm2"
        assert merged.match_keys == "2012arc_qm1,2012arc_qm2,2012arc_qm3"
        # Untouched rows get their single match key backfilled.
        other = session.exec(
            select(Video).where(Video.youtube_id == "AAAAAAAAAAA")
        ).first()
        assert other.match_keys == "2012arc_qm4"
        # The duplicates' jobs are gone with them.
        jobs = session.exec(select(DownloadJob)).all()
        assert sorted(j.video_id for j in jobs) == sorted(v.id for v in vids)

    # Idempotent.
    _merge_suffixed_youtube_ids()
    with Session(get_engine()) as session:
        assert len(session.exec(select(Video)).all()) == 2


def test_null_match_keys_backfilled_to_empty_string(tmp_path, monkeypatch):
    """Reproduces upgrading a real pre-existing database, where ``match_keys``
    was added via ``ALTER TABLE ... ADD COLUMN`` (nullable, no default) rather
    than ``create_all``'s NOT NULL column. A row whose ``match_key`` was also
    NULL (e.g. an event_vod video, which never sets one) fell through the
    match_key-driven backfill and stayed NULL, crashing `_upsert`'s
    `video.match_keys.split(",")` the first time a later scan merged a real
    match_key onto it.
    """
    import sqlite3

    import app.db as db

    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db_path))
    # Deliberately omits match_keys, matching a DB from before that column
    # existed; _migrate()'s ALTER TABLE step is what adds it.
    conn.execute(
        "CREATE TABLE video (id INTEGER PRIMARY KEY, youtube_id TEXT, "
        "match_key TEXT, status TEXT, file_path TEXT)"
    )
    conn.execute("CREATE TABLE download_job (id INTEGER PRIMARY KEY)")
    conn.execute(
        "CREATE TABLE source (id INTEGER PRIMARY KEY, kind TEXT, start_year INTEGER)"
    )
    conn.execute(
        "INSERT INTO video (youtube_id, match_key, status, file_path) "
        "VALUES ('AAAAAAAAAAA', NULL, 'completed', NULL)"
    )
    conn.commit()
    conn.close()

    monkeypatch.setenv("ARCHIVER_DB_URL", f"sqlite:///{db_path}")
    db._engine = None
    try:
        db._migrate()

        with sqlite3.connect(str(db_path)) as conn:
            row = conn.execute(
                "SELECT match_keys FROM video WHERE youtube_id = 'AAAAAAAAAAA'"
            ).fetchone()
        assert row[0] == ""
    finally:
        db._engine = None


def test_reclassify_unavailable_failures(temp_env):
    from app.db import _reclassify_unavailable_failures, get_engine
    from app.models import Video, VideoStatus

    with Session(get_engine()) as session:
        _add(
            session,
            "AAAAAAAAAAA",
            "2012arc_qm1",
            "failed",
            error="ERROR: [youtube] AAAAAAAAAAA: Private video",
        )
        _add(
            session,
            "BBBBBBBBBBB",
            "2012arc_qm2",
            "failed",
            error="ERROR: [youtube] BBBBBBBBBBB: This video is unavailable",
        )
        # Old bot-check failure -- should stay failed, it can succeed on retry.
        _add(
            session,
            "CCCCCCCCCCC",
            "2012arc_qm3",
            "failed",
            error="ERROR: [youtube] CCCCCCCCCCC: Sign in to confirm you're not a bot.",
        )

    _reclassify_unavailable_failures()

    with Session(get_engine()) as session:
        by_id = {v.youtube_id: v for v in session.exec(select(Video)).all()}
        assert by_id["AAAAAAAAAAA"].status == VideoStatus.unavailable
        assert by_id["BBBBBBBBBBB"].status == VideoStatus.unavailable
        assert by_id["CCCCCCCCCCC"].status == VideoStatus.failed

    # Idempotent.
    _reclassify_unavailable_failures()
    with Session(get_engine()) as session:
        by_id = {v.youtube_id: v for v in session.exec(select(Video)).all()}
        assert by_id["AAAAAAAAAAA"].status == VideoStatus.unavailable


def test_legacy_team_sources_backfill_to_all_years_ongoing(temp_env):
    from app.db import _backfill_team_source_years, get_engine
    from app.models import Source, SourceKind

    with Session(get_engine()) as session:
        session.add(Source(kind=SourceKind.team, value="frc2713"))
        session.add(Source(kind=SourceKind.season, value="2026"))
        session.commit()

    _backfill_team_source_years()

    with Session(get_engine()) as session:
        team = session.exec(select(Source).where(Source.value == "frc2713")).first()
        assert team.start_year == 1992
        assert team.end_year is None
        season = session.exec(select(Source).where(Source.value == "2026")).first()
        assert season.start_year is None

    # Idempotent: a narrowed range survives a second run.
    with Session(get_engine()) as session:
        team = session.exec(select(Source).where(Source.value == "frc2713")).first()
        team.start_year = 2022
        session.add(team)
        session.commit()

    _backfill_team_source_years()

    with Session(get_engine()) as session:
        team = session.exec(select(Source).where(Source.value == "frc2713")).first()
        assert team.start_year == 2022
