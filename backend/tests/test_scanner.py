"""Discovery-scan tests against a fake TBA client (no network)."""

from datetime import date, timedelta

from sqlmodel import Session, select


def _future_date():
    return (date.today() + timedelta(days=3)).isoformat()


def _past_date():
    return (date.today() - timedelta(days=30)).isoformat()


class FakeTba:
    """Stand-in for TbaClient with canned responses."""

    def __init__(self):
        self.team_match_years = []
        self.season_years = []
        self.participated = list(range(1992, date.today().year + 1))
        self.ended_event = {
            "key": "2026test",
            "name": "Test Event",
            "year": 2026,
            "end_date": _past_date(),
            "state_prov": "New Hampshire",
            "event_type": 0,
            "webcasts": [
                {"type": "youtube", "channel": "AAAAAAAAAAA"},
                {"type": "youtube", "channel": "BBBBBBBBBBB"},
                {"type": "twitch", "channel": "some_twitch"},  # ignored
            ],
        }
        self.live_event = {
            "key": "2026live",
            "name": "Live Event",
            "year": 2026,
            "end_date": _future_date(),
            "state_prov": "New Hampshire",
            "event_type": 0,
            "webcasts": [{"type": "youtube", "channel": "CCCCCCCCCCC"}],
        }
        # Out-of-region regional: excluded by the season scope filter.
        self.regional_event = {
            "key": "2026reg",
            "name": "Regional Event",
            "year": 2026,
            "end_date": _past_date(),
            "state_prov": "California",
            "event_type": 0,
            "webcasts": [{"type": "youtube", "channel": "EEEEEEEEEEE"}],
        }
        # Out-of-region championship: included despite the state via event_type.
        self.champs_event = {
            "key": "2026cmp",
            "name": "Champs Event",
            "year": 2026,
            "end_date": _past_date(),
            "state_prov": "Texas",
            "event_type": 4,
            "webcasts": [{"type": "youtube", "channel": "FFFFFFFFFFF"}],
        }

    def season_events(self, year):
        self.season_years.append(year)
        return [
            self.ended_event,
            self.live_event,
            self.regional_event,
            self.champs_event,
        ]

    def district_events(self, district_key):
        return [self.ended_event]

    def event_matches(self, event_key):
        return [
            {
                "key": f"{event_key}_qm1",
                "event_key": event_key,
                "videos": [{"type": "youtube", "key": "GGGGGGGGGGG"}],
            }
        ]

    def team_matches(self, team_key, year):
        self.team_match_years.append(year)
        return [
            {
                "key": f"{year}test_qm1",
                "event_key": f"{year}test",
                "videos": [
                    {"type": "youtube", "key": f"D{year}DDDDDD"},
                    {"type": "tba", "key": "ignored"},
                ],
            }
        ]

    def years_participated(self, team_key):
        return self.participated


def test_event_has_ended_counts_from_end_date(temp_env):
    from app.services.scanner import _event_has_ended

    today = date(2026, 4, 12)
    assert _event_has_ended("2026-04-12", 0, today)
    assert not _event_has_ended("2026-04-13", 0, today)
    assert _event_has_ended("2026-04-11", 1, today)
    assert not _event_has_ended("2026-04-12", 1, today)


def _add_source(session, kind, value, start_year=None, end_year=None):
    from app.models import Source, SourceKind

    session.add(
        Source(
            kind=SourceKind(kind),
            value=value,
            start_year=start_year,
            end_year=end_year,
        )
    )
    session.commit()


def test_season_scan_extracts_ended_vods_only(temp_env):
    from app.db import get_engine
    from app.models import Video, VideoStatus
    from app.services.scanner import Scanner

    with Session(get_engine()) as session:
        _add_source(session, "season", "2026")
        run = Scanner(session, client=FakeTba()).run()
        assert run.ok

        ids = {v.youtube_id for v in session.exec(select(Video)).all()}
        # Ended NE VODs (A, B) and the ended out-of-region championship (F) are
        # included, plus the match video (G) from those in-scope ended events;
        # the live NE event, its twitch webcast, and the ended out-of-region
        # *regional* event are excluded.
        assert ids == {"AAAAAAAAAAA", "BBBBBBBBBBB", "FFFFFFFFFFF", "GGGGGGGGGGG"}
        for v in session.exec(select(Video)).all():
            assert v.status == VideoStatus.queued


def test_season_scan_excludes_out_of_region_non_champs(temp_env):
    from app.db import get_engine
    from app.models import Video
    from app.services.scanner import Scanner

    with Session(get_engine()) as session:
        _add_source(session, "season", "2026")
        Scanner(session, client=FakeTba()).run()

        ids = {v.youtube_id for v in session.exec(select(Video)).all()}
        assert "EEEEEEEEEEE" not in ids


def test_season_scan_also_ingests_matches(temp_env):
    from app.db import get_engine
    from app.models import SourceType, Video
    from app.services.scanner import Scanner

    with Session(get_engine()) as session:
        _add_source(session, "season", "2026")
        Scanner(session, client=FakeTba()).run()

        matches = [
            v
            for v in session.exec(select(Video)).all()
            if v.source_type == SourceType.event_match
        ]
        # In-scope ended events (NH regional + TX championship) contribute the
        # match video; the live event and the out-of-region regional do not.
        assert {v.youtube_id for v in matches} == {"GGGGGGGGGGG"}
        assert matches[0].match_key.endswith("_qm1")


def test_event_type_scan_extracts_matching_events(temp_env):
    from app.db import get_engine
    from app.models import SourceType, Video
    from app.services.scanner import Scanner

    with Session(get_engine()) as session:
        _add_source(session, "event_type", "3,4")
        run = Scanner(session, client=FakeTba()).run()
        assert run.ok

        vids = session.exec(select(Video)).all()
        # Only the ended type-4 champs event yields a match video; the type-0
        # events are ignored regardless of region.
        assert {v.youtube_id for v in vids} == {"GGGGGGGGGGG"}
        v = vids[0]
        assert v.source_type == SourceType.event_match
        assert v.match_key == "2026cmp_qm1"
        assert v.year == 2026

        assert Scanner(session, client=FakeTba()).run().enqueued == 0


def test_team_scan_extracts_match_videos(temp_env):
    from app.db import get_engine
    from app.models import Video
    from app.services.scanner import Scanner

    with Session(get_engine()) as session:
        _add_source(session, "team", "frc254", start_year=2026, end_year=2026)
        Scanner(session, client=FakeTba()).run()
        vids = session.exec(select(Video)).all()
        keys = {v.youtube_id for v in vids}
        assert "D2026DDDDDD" in keys
        match_video = next(v for v in vids if v.youtube_id == "D2026DDDDDD")
        assert match_video.match_key == "2026test_qm1"
        assert "frc254" in match_video.team_keys


def test_team_scan_honors_year_range(temp_env):
    from app.db import get_engine
    from app.services.scanner import Scanner

    client = FakeTba()
    with Session(get_engine()) as session:
        _add_source(session, "team", "frc254", start_year=2022, end_year=2023)
        Scanner(session, client=client).run()
    assert client.team_match_years == [2023, 2022]


def test_team_scan_open_ended_range_runs_to_current_year(temp_env):
    from app.db import get_engine
    from app.services.scanner import Scanner

    client = FakeTba()
    with Session(get_engine()) as session:
        _add_source(session, "team", "frc254", start_year=2024)
        Scanner(session, client=client).run()
    assert client.team_match_years == list(range(date.today().year, 2023, -1))


def test_season_sources_scan_newest_first(temp_env):
    from app.db import get_engine
    from app.services.scanner import Scanner

    client = FakeTba()
    with Session(get_engine()) as session:
        for year in ("2024", "2026", "2025"):
            _add_source(session, "season", year)
        Scanner(session, client=client).run()
    assert client.season_years == [2026, 2025, 2024]


def test_team_scan_skips_years_the_team_did_not_play(temp_env):
    from app.db import get_engine
    from app.services.scanner import Scanner

    client = FakeTba()
    client.participated = [2023]
    with Session(get_engine()) as session:
        _add_source(session, "team", "frc254", start_year=2022, end_year=2024)
        Scanner(session, client=client).run()
    assert client.team_match_years == [2023]


def test_dedup_second_scan_enqueues_nothing(temp_env):
    from app.db import get_engine
    from app.models import DownloadJob
    from app.services.scanner import Scanner

    with Session(get_engine()) as session:
        _add_source(session, "season", "2026")
        first = Scanner(session, client=FakeTba()).run()
        assert first.enqueued == 4

        second = Scanner(session, client=FakeTba()).run()
        assert second.enqueued == 0
        # No duplicate jobs created.
        jobs = session.exec(select(DownloadJob)).all()
        assert len(jobs) == 4


def test_force_redownload_requeues_single(temp_env):
    from app.db import get_engine
    from app.models import Video
    from app.services.scanner import Scanner

    with Session(get_engine()) as session:
        _add_source(session, "season", "2026")
        Scanner(session, client=FakeTba()).run()

        v = session.exec(select(Video).where(Video.youtube_id == "AAAAAAAAAAA")).first()
        # Simulate it having completed, then a manual force.
        from app.models import VideoStatus, JobState, DownloadJob

        v.status = VideoStatus.completed
        v.force_redownload = True
        for job in session.exec(
            select(DownloadJob).where(DownloadJob.video_id == v.id)
        ).all():
            job.state = JobState.done
        session.add(v)
        session.commit()

        run = Scanner(session, client=FakeTba()).run()
        assert run.enqueued == 1


class SharedVideoTba(FakeTba):
    """One stream listed on three matches, each at its own timestamp."""

    def event_matches(self, event_key):
        return [
            {
                "key": f"{event_key}_qm{n}",
                "event_key": event_key,
                "videos": [{"type": "youtube", "key": f"HHHHHHHHHHH?t={n * 100}"}],
            }
            for n in (1, 2, 3)
        ]


class AlreadyDownloadedTba(FakeTba):
    """The team's match video is also listed on the event's own match feed,
    same as real TBA data — a season scan discovers it as ``event_match``
    before any team source ever sees it."""

    def event_matches(self, event_key):
        if event_key == "2026test":
            return [
                {
                    "key": "2026test_qm1",
                    "event_key": event_key,
                    "videos": [{"type": "youtube", "key": "D2026DDDDDD"}],
                }
            ]
        return super().event_matches(event_key)


def test_team_scan_copies_already_downloaded_video_for_team(temp_env):
    from app.db import get_engine, get_setting
    from app.models import Video, VideoStatus
    from app.services.scanner import Scanner
    from pathlib import Path

    client = AlreadyDownloadedTba()
    with Session(get_engine()) as session:
        _add_source(session, "season", "2026")
        Scanner(session, client=client).run()

        video = session.exec(
            select(Video).where(Video.youtube_id == "D2026DDDDDD")
        ).first()
        assert video is not None
        assert video.source_type.value == "event_match"

        # Simulate the season-discovered video having already been downloaded,
        # same as a real worker run would leave it.
        media_root = Path(get_setting(session, "media_root"))
        dest = media_root / "2026" / "2026test" / "matches"
        dest.mkdir(parents=True)
        source = dest / "2026test_qm1 [D2026DDDDDD].mkv"
        source.write_bytes(b"video")
        video.status = VideoStatus.completed
        video.file_path = str(source)
        session.add(video)
        session.commit()

        _add_source(session, "team", "frc254", start_year=2026, end_year=2026)
        Scanner(session, client=client).run()

        copy = (
            media_root
            / "frc254"
            / "2026"
            / "2026test"
            / "2026test_qm1 [D2026DDDDDD].mkv"
        )
        assert copy.read_bytes() == b"video"

        video = session.exec(
            select(Video).where(Video.youtube_id == "D2026DDDDDD")
        ).first()
        assert "frc254" in video.team_keys.split(",")
        # The already-completed video is not re-enqueued for download.
        assert video.status == VideoStatus.completed


def test_timestamped_keys_collapse_to_one_video(temp_env):
    from app.db import get_engine
    from app.models import DownloadJob, Video
    from app.services.scanner import Scanner

    with Session(get_engine()) as session:
        _add_source(session, "event_type", "4")
        run = Scanner(session, client=SharedVideoTba()).run()

        vids = session.exec(select(Video)).all()
        assert len(vids) == 1
        assert vids[0].youtube_id == "HHHHHHHHHHH"
        assert vids[0].match_key == "2026cmp_qm1"
        assert vids[0].match_keys == "2026cmp_qm1,2026cmp_qm2,2026cmp_qm3"
        assert vids[0].webpage_url.endswith("HHHHHHHHHHH")
        assert run.enqueued == 1
        assert len(session.exec(select(DownloadJob)).all()) == 1
