"""Ganymede reconcile tests against a fake TBA client and a fake Ganymede client.

Ganymede is the source of truth for "already tracked" — these tests assert
the sync only ever *adds*, is idempotent against Ganymede's current state,
bypasses the YouTube path's ended-event gate, and never issues a delete-style
call no matter how the sets diverge.
"""

from datetime import date, timedelta

from sqlmodel import Session


def _future_date():
    return (date.today() + timedelta(days=3)).isoformat()


def _past_date():
    return (date.today() - timedelta(days=30)).isoformat()


class FakeTba:
    """Stand-in for TbaClient with canned events, mirroring test_scanner.py."""

    def __init__(self):
        self.ended_event = {
            "key": "2026test",
            "name": "Test Event",
            "year": 2026,
            "end_date": _past_date(),
            "state_prov": "New Hampshire",
            "event_type": 0,
            "webcasts": [
                {"type": "youtube", "channel": "AAAAAAAAAAA"},
                {"type": "twitch", "channel": "Frc_Ended"},
            ],
        }
        # Live/upcoming event: excluded from the YouTube scan (not yet ended)
        # but its Twitch channel must still be tracked by the Ganymede sync.
        self.live_event = {
            "key": "2026live",
            "name": "Live Event",
            "year": 2026,
            "end_date": _future_date(),
            "state_prov": "New Hampshire",
            "event_type": 0,
            "webcasts": [{"type": "twitch", "channel": "frc_live"}],
        }
        self.regional_event = {
            "key": "2026reg",
            "name": "Regional Event",
            "year": 2026,
            "end_date": _past_date(),
            "state_prov": "California",
            "event_type": 0,
            "webcasts": [{"type": "twitch", "channel": "frc_regional"}],
        }

    def season_events(self, year):
        return [self.ended_event, self.live_event, self.regional_event]

    def district_events(self, district_key):
        return [self.ended_event]


class FakeGanymede:
    """Records calls; never exposes/executes anything delete-shaped."""

    def __init__(self, already_watched=()):
        self._watched_logins = set(already_watched)
        self.calls: list[tuple[str, tuple]] = []
        self.fail_on_add = set()

    def get_channel_by_name(self, name):
        self.calls.append(("get_channel_by_name", (name,)))
        return {"id": f"chan-{name}"} if name in self._watched_logins else None

    def archive_channel(self, name):
        self.calls.append(("archive_channel", (name,)))
        return {"id": f"chan-{name}"}

    def list_watched(self):
        self.calls.append(("list_watched", ()))
        return [
            {"edges": {"channel": {"name": login}}} for login in self._watched_logins
        ]

    def add_watched(self, channel_id, **kwargs):
        self.calls.append(("add_watched", (channel_id,)))
        if channel_id in self.fail_on_add:
            from app.services.ganymede_client import GanymedeError

            raise GanymedeError("boom")
        login = channel_id.removeprefix("chan-")
        self._watched_logins.add(login)
        return {"id": f"live-{channel_id}"}


def _add_source(session, kind, value):
    from app.models import Source, SourceKind

    session.add(Source(kind=SourceKind(kind), value=value))
    session.commit()


def _settings(**overrides):
    base = {
        "tba_api_key": "x",
        "ganymede_base_url": "http://gany.test/api/v1",
        "ganymede_api_key": "y",
        "ganymede_resolution": "best",
        "ganymede_vod_resolution": "best",
        "ganymede_watch_vod": True,
        "ganymede_archive_chat": False,
    }
    base.update(overrides)
    return base


NON_MUTATING = {"get_channel_by_name", "list_watched"}


def _assert_no_deletes(gany: FakeGanymede):
    for name, _ in gany.calls:
        assert name in NON_MUTATING or name in (
            "archive_channel",
            "add_watched",
        ), f"unexpected call {name} — only GET/add-only POSTs are permitted"


def test_no_delete_method_exists_on_client():
    from app.services.ganymede_client import GanymedeClient

    for attr in dir(GanymedeClient):
        assert "delete" not in attr.lower()
        assert not attr.lower().startswith("remove")


def test_list_watched_unwraps_data_envelope(monkeypatch):
    """Ganymede's real GET /live wraps the array in {"success", "data"} —
    list_watched() must return the inner array, not the envelope dict (whose
    iterated keys would be plain strings and break _current_logins)."""
    import httpx

    from app.services.ganymede_client import GanymedeClient

    class FakeResponse:
        status_code = 200

        def json(self):
            return {
                "success": True,
                "data": [{"edges": {"channel": {"name": "frc_test"}}}],
            }

    monkeypatch.setattr(httpx, "get", lambda *a, **k: FakeResponse())

    client = GanymedeClient("http://gany.test/api/v1", "key")
    watched = client.list_watched()

    assert watched == [{"edges": {"channel": {"name": "frc_test"}}}]


def test_archive_channel_unwraps_data_envelope(monkeypatch):
    """POST /archive/channel also wraps its response in {"success", "data"} —
    archive_channel() must return the inner channel dict so callers can index
    channel["id"] directly, as GanymedeSync._track does."""
    import httpx

    from app.services.ganymede_client import GanymedeClient

    class FakeResponse:
        status_code = 200

        def json(self):
            return {
                "success": True,
                "data": {"id": "chan-123", "name": "nycfirst"},
                "message": "channel",
            }

    monkeypatch.setattr(httpx, "post", lambda *a, **k: FakeResponse())

    client = GanymedeClient("http://gany.test/api/v1", "key")
    channel = client.archive_channel("nycfirst")

    assert channel["id"] == "chan-123"


def test_add_watched_sends_required_clips_fields(monkeypatch):
    """Ganymede's validator rejects POST /live with a 400 ('gte' tag) unless
    clips_limit/clips_interval_days are >= 1, even though watch_clips is
    always False here — add_watched() must send non-zero defaults for them."""
    import httpx

    from app.services.ganymede_client import GanymedeClient

    captured = {}

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"success": True, "data": {"id": "live-1"}, "message": "live"}

    def fake_post(url, headers, json, timeout):
        captured.update(json)
        return FakeResponse()

    monkeypatch.setattr(httpx, "post", fake_post)

    client = GanymedeClient("http://gany.test/api/v1", "key")
    client.add_watched(
        "chan-123",
        resolution="best",
        vod_resolution="best",
        watch_live=True,
        watch_vod=True,
        download_archives=True,
        archive_chat=False,
        render_chat=False,
    )

    assert captured["clips_limit"] >= 1
    assert captured["clips_interval_days"] >= 1


def test_tracks_twitch_channel_including_future_and_ended_events(temp_env):
    from app.db import get_engine
    from app.services.ganymede_sync import GanymedeSync

    with Session(get_engine()) as session:
        _add_source(session, "season", "2026")
        tba = FakeTba()
        gany = FakeGanymede()

        result = GanymedeSync(
            session, _settings(), tba_client=tba, gany_client=gany
        ).run()

        # Ended NE event's twitch channel, and the *future* NE event's twitch
        # channel, are both tracked — the ended-gate does not apply here.
        # The out-of-region regional event is excluded by _event_in_scope.
        assert result.desired == 2
        assert result.added == 2
        assert result.errors == 0
        assert gany._watched_logins == {"frc_ended", "frc_live"}
        assert "frc_regional" not in gany._watched_logins
        _assert_no_deletes(gany)


def test_idempotent_reruns_make_no_new_ganymede_writes(temp_env):
    from app.db import get_engine
    from app.services.ganymede_sync import GanymedeSync

    with Session(get_engine()) as session:
        _add_source(session, "season", "2026")
        tba = FakeTba()
        gany = FakeGanymede()

        GanymedeSync(session, _settings(), tba_client=tba, gany_client=gany).run()
        writes_after_first = [
            c for c in gany.calls if c[0] in ("archive_channel", "add_watched")
        ]
        # archive_channel + add_watched for each of the 2 desired logins.
        assert len(writes_after_first) == 4

        gany.calls.clear()
        result = GanymedeSync(
            session, _settings(), tba_client=FakeTba(), gany_client=gany
        ).run()

        assert result.added == 0
        writes_after_second = [
            c for c in gany.calls if c[0] in ("archive_channel", "add_watched")
        ]
        assert writes_after_second == []
        _assert_no_deletes(gany)


def test_already_watched_channel_is_skipped(temp_env):
    from app.db import get_engine
    from app.services.ganymede_sync import GanymedeSync

    with Session(get_engine()) as session:
        _add_source(session, "district", "2026ne")
        tba = FakeTba()
        gany = FakeGanymede(already_watched={"frc_ended"})

        result = GanymedeSync(
            session, _settings(), tba_client=tba, gany_client=gany
        ).run()

        assert result.desired == 1
        assert result.added == 0
        add_calls = [c for c in gany.calls if c[0] == "add_watched"]
        assert add_calls == []
        _assert_no_deletes(gany)


def test_non_twitch_and_out_of_scope_contribute_nothing(temp_env):
    from app.db import get_engine
    from app.services.ganymede_sync import GanymedeSync

    with Session(get_engine()) as session:
        _add_source(session, "season", "2026")
        tba = FakeTba()
        gany = FakeGanymede()

        result = GanymedeSync(
            session, _settings(), tba_client=tba, gany_client=gany
        ).run()
        assert "frc_regional" not in gany._watched_logins


def test_one_channel_error_does_not_abort_the_run(temp_env):
    from app.db import get_engine
    from app.services.ganymede_sync import GanymedeSync

    with Session(get_engine()) as session:
        _add_source(session, "season", "2026")
        tba = FakeTba()
        gany = FakeGanymede()
        gany.fail_on_add = {"chan-frc_ended"}

        result = GanymedeSync(
            session, _settings(), tba_client=tba, gany_client=gany
        ).run()

        assert result.desired == 2
        assert result.added == 1  # frc_live succeeds despite frc_ended failing
        assert result.errors == 1
        assert "frc_live" in gany._watched_logins
        assert "frc_ended" not in gany._watched_logins
        _assert_no_deletes(gany)
