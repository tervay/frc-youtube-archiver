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
        self.ended_event = {
            "key": "2026test", "name": "Test Event", "year": 2026,
            "end_date": _past_date(), "state_prov": "New Hampshire", "event_type": 0,
            "webcasts": [
                {"type": "youtube", "channel": "AAAAAAAAAAA"},
                {"type": "youtube", "channel": "BBBBBBBBBBB"},
                {"type": "twitch", "channel": "some_twitch"},  # ignored
            ],
        }
        self.live_event = {
            "key": "2026live", "name": "Live Event", "year": 2026,
            "end_date": _future_date(), "state_prov": "New Hampshire", "event_type": 0,
            "webcasts": [{"type": "youtube", "channel": "CCCCCCCCCCC"}],
        }
        # Out-of-region regional: excluded by the season scope filter.
        self.regional_event = {
            "key": "2026reg", "name": "Regional Event", "year": 2026,
            "end_date": _past_date(), "state_prov": "California", "event_type": 0,
            "webcasts": [{"type": "youtube", "channel": "EEEEEEEEEEE"}],
        }
        # Out-of-region championship: included despite the state via event_type.
        self.champs_event = {
            "key": "2026cmp", "name": "Champs Event", "year": 2026,
            "end_date": _past_date(), "state_prov": "Texas", "event_type": 4,
            "webcasts": [{"type": "youtube", "channel": "FFFFFFFFFFF"}],
        }

    def season_event_keys(self, year):
        return ["2026test", "2026live", "2026reg", "2026cmp"]

    def event(self, key):
        return {
            "2026test": self.ended_event,
            "2026live": self.live_event,
            "2026reg": self.regional_event,
            "2026cmp": self.champs_event,
        }.get(key)

    def district_events(self, district_key):
        return [self.ended_event]

    def team_matches(self, team_key, year):
        return [{
            "key": "2026test_qm1", "event_key": "2026test",
            "videos": [
                {"type": "youtube", "key": "DDDDDDDDDDD"},
                {"type": "tba", "key": "ignored"},
            ],
        }]


def _add_source(session, kind, value):
    from app.models import Source, SourceKind
    session.add(Source(kind=SourceKind(kind), value=value))
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
        # included; the live NE event, its twitch webcast, and the ended
        # out-of-region *regional* event are excluded.
        assert ids == {"AAAAAAAAAAA", "BBBBBBBBBBB", "FFFFFFFFFFF"}
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


def test_team_scan_extracts_match_videos(temp_env):
    from app.db import get_engine
    from app.models import Video
    from app.services.scanner import Scanner

    with Session(get_engine()) as session:
        _add_source(session, "team", "frc254")
        Scanner(session, client=FakeTba()).run()
        vids = session.exec(select(Video)).all()
        keys = {v.youtube_id for v in vids}
        assert "DDDDDDDDDDD" in keys
        match_video = next(v for v in vids if v.youtube_id == "DDDDDDDDDDD")
        assert match_video.match_key == "2026test_qm1"
        assert "frc254" in match_video.team_keys


def test_dedup_second_scan_enqueues_nothing(temp_env):
    from app.db import get_engine
    from app.models import DownloadJob
    from app.services.scanner import Scanner

    with Session(get_engine()) as session:
        _add_source(session, "season", "2026")
        first = Scanner(session, client=FakeTba()).run()
        assert first.enqueued == 3

        second = Scanner(session, client=FakeTba()).run()
        assert second.enqueued == 0
        # No duplicate jobs created.
        jobs = session.exec(select(DownloadJob)).all()
        assert len(jobs) == 3


def test_force_redownload_requeues_single(temp_env):
    from app.db import get_engine
    from app.models import Video
    from app.services.scanner import Scanner

    with Session(get_engine()) as session:
        _add_source(session, "season", "2026")
        Scanner(session, client=FakeTba()).run()

        v = session.exec(
            select(Video).where(Video.youtube_id == "AAAAAAAAAAA")
        ).first()
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
