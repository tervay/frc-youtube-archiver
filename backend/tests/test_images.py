from datetime import date, timedelta
from pathlib import Path

import httpx
import pytest
from sqlmodel import Session, select

YEAR = date.today().year


class FakeTba:
    def __init__(self, media):
        self.media = media
        self.media_calls = []

    def event_keys(self, year):
        return sorted(k for k in self.media if k.startswith(str(year)))

    def event_team_media(self, event_key, max_age_days=None):
        self.media_calls.append((event_key, max_age_days))
        return self.media[event_key]


def _imgur(key, *teams, preferred=False):
    return {
        "type": "imgur",
        "foreign_key": key,
        "preferred": preferred,
        "team_keys": list(teams),
    }


def _images(session):
    from app.models import Image

    return session.exec(select(Image).order_by(Image.id)).all()


def _scan(session, tba):
    from app.services.image_scanner import ImageScanner

    return ImageScanner(session, client=tba).run()


def test_discovers_only_image_media_and_dedups(temp_env):
    from app.db import get_engine

    tba = FakeTba(
        {
            f"{YEAR}nhgrs": [
                _imgur("AAAAAAA", "frc2713", preferred=True),
                _imgur("CCCCCCC", "frc2713", "frc1153"),
                {"type": "avatar", "foreign_key": "avatar_x", "team_keys": ["frc2713"]},
                {
                    "type": "youtube",
                    "foreign_key": "Xsur2oiqmjk",
                    "team_keys": ["frc2713"],
                },
                {
                    "type": "cdphotothread",
                    "foreign_key": "123",
                    "team_keys": ["frc2713"],
                },
            ],
            f"{YEAR}necmp": [_imgur("AAAAAAA", "frc2713", preferred=True)],
            f"{YEAR - 1}cacc": [_imgur("BBBBBBB", "frc254")],
        }
    )
    with Session(get_engine()) as session:
        run = _scan(session, tba)
        assert run.ok and run.discovered == 4
        rows = _images(session)
        assert sorted(
            (r.foreign_key, r.team_number, r.year, r.preferred) for r in rows
        ) == [
            ("AAAAAAA", 2713, YEAR, True),
            ("BBBBBBB", 254, YEAR - 1, False),
            ("CCCCCCC", 1153, YEAR, False),
            ("CCCCCCC", 2713, YEAR, False),
        ]
        assert _scan(session, tba).discovered == 0
        assert len(_images(session)) == 4


def test_past_years_use_recheck_window(temp_env):
    from app.db import get_engine

    tba = FakeTba({f"{YEAR}nhgrs": [], f"{YEAR - 1}nhgrs": []})
    with Session(get_engine()) as session:
        _scan(session, tba)
    assert tba.media_calls == [(f"{YEAR}nhgrs", None), (f"{YEAR - 1}nhgrs", 30)]


def test_tba_client_serves_fresh_cache_without_http(temp_env, monkeypatch):
    from app.db import get_engine
    from app.models import TbaCache, utcnow
    from app.tba.client import TbaClient

    def boom(*args, **kwargs):
        raise AssertionError("no HTTP expected")

    monkeypatch.setattr(httpx, "get", boom)
    with Session(get_engine()) as session:
        session.add(
            TbaCache(path="/event/2019nhgrs/team_media", body="[]", fetched_at=utcnow())
        )
        session.add(
            TbaCache(
                path="/event/2018nhgrs/team_media",
                body="[]",
                fetched_at=utcnow() - timedelta(days=31),
            )
        )
        session.commit()
        client = TbaClient("key", session)
        assert client.event_team_media("2019nhgrs", 30) == []
        with pytest.raises(AssertionError):
            client.event_team_media("2018nhgrs", 30)


@pytest.fixture()
def media(temp_env):
    from app.db import get_engine, set_setting

    root = Path(temp_env) / "media"
    with Session(get_engine()) as session:
        set_setting(session, "media_root", str(root))
        session.commit()
    return root


class FakeImgur:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, headers, follow_redirects, timeout):
        self.calls.append((url, headers, follow_redirects))
        status, resp_headers, body = self.responses.pop(0)
        return httpx.Response(status, headers=resp_headers, content=body)


def _pending(session, key="AAAAAAA", team=2713, year=2026):
    from app.models import Image

    image = Image(provider="imgur", foreign_key=key, team_number=team, year=year)
    session.add(image)
    session.commit()
    return image


def _download(monkeypatch, fake):
    from app.services import image_downloader

    monkeypatch.setattr(image_downloader.httpx, "get", fake)
    return image_downloader.download_next()


def test_downloads_with_content_type_extension(media, monkeypatch):
    from app.db import get_engine
    from app.models import Image, ImageStatus

    with Session(get_engine()) as session:
        image_id = _pending(session).id
    fake = FakeImgur([(200, {"content-type": "image/png"}, b"png-bytes")])
    assert _download(monkeypatch, fake) == 2.0

    url, headers, follow = fake.calls[0]
    assert url == "https://i.imgur.com/AAAAAAA.jpg"
    assert headers["User-Agent"] == "curl/8.5.0"
    assert follow is False

    target = media / "images" / "2713" / "2026" / "AAAAAAA.png"
    assert target.read_bytes() == b"png-bytes"
    assert not list(target.parent.glob("*.part"))
    with Session(get_engine()) as session:
        image = session.get(Image, image_id)
        assert image.status == ImageStatus.downloaded
        assert (image.ext, image.size, image.file_path) == ("png", 9, str(target))


def test_redirect_marks_removed(media, monkeypatch):
    from app.db import get_engine
    from app.models import Image, ImageStatus

    with Session(get_engine()) as session:
        image_id = _pending(session).id
    fake = FakeImgur([(302, {"location": "https://i.imgur.com/removed.png"}, b"")])
    _download(monkeypatch, fake)
    with Session(get_engine()) as session:
        assert session.get(Image, image_id).status == ImageStatus.removed


def test_rate_limit_keeps_pending_without_attempt(media, monkeypatch):
    from app.db import get_engine
    from app.models import Image, ImageStatus
    from app.services.image_downloader import RateLimited

    with Session(get_engine()) as session:
        image_id = _pending(session).id
    fake = FakeImgur([(429, {"retry-after": "0"}, b"")])
    with pytest.raises(RateLimited) as exc:
        _download(monkeypatch, fake)
    assert exc.value.retry_after == 0
    with Session(get_engine()) as session:
        image = session.get(Image, image_id)
        assert (image.status, image.attempts) == (ImageStatus.pending, 0)


def test_server_errors_fail_after_max_retries(media, monkeypatch):
    from app.db import get_engine
    from app.models import Image, ImageStatus

    with Session(get_engine()) as session:
        image_id = _pending(session).id
    fake = FakeImgur([(500, {}, b"")] * 4)
    for _ in range(3):
        _download(monkeypatch, fake)
        with Session(get_engine()) as session:
            assert session.get(Image, image_id).status == ImageStatus.pending
    _download(monkeypatch, fake)
    with Session(get_engine()) as session:
        image = session.get(Image, image_id)
        assert (image.status, image.attempts) == (ImageStatus.failed, 4)


def test_same_image_on_two_team_years_downloads_once(media, monkeypatch):
    from app.db import get_engine

    with Session(get_engine()) as session:
        _pending(session, year=2025)
        _pending(session, year=2026)
    fake = FakeImgur([(200, {"content-type": "image/jpeg"}, b"jpg")])
    _download(monkeypatch, fake)
    assert _download(monkeypatch, fake) == 0.0
    assert len(fake.calls) == 1
    root = media / "images" / "2713"
    assert (root / "2025" / "AAAAAAA.jpg").read_bytes() == b"jpg"
    assert (root / "2026" / "AAAAAAA.jpg").read_bytes() == b"jpg"


def test_idle_when_disabled(temp_env, monkeypatch):
    from app.db import get_engine, set_setting

    with Session(get_engine()) as session:
        _pending(session)
        set_setting(session, "images_enabled", False)
        session.commit()
    assert _download(monkeypatch, FakeImgur([])) is None


def test_reconciler_ignores_images(media):
    from app.db import get_engine
    from app.services.reconciler import reconcile

    target = media / "images" / "2713" / "2026" / "AAAAAAA.jpg"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"jpg")
    with Session(get_engine()) as session:
        reconcile(session)
    assert target.exists()


def test_list_images_counts_and_filters(temp_env):
    from app.api.images import list_images
    from app.db import get_engine
    from app.models import ImageStatus

    with Session(get_engine()) as session:
        _pending(session, key="AAAAAAA", team=2713)
        removed = _pending(session, key="BBBBBBB", team=254)
        removed.status = ImageStatus.removed
        session.add(removed)
        session.commit()

        everything = list_images(session=session, page=1, page_size=60)
        assert everything["total"] == 2
        assert everything["counts"] == {
            "pending": 1,
            "downloaded": 0,
            "removed": 1,
            "failed": 0,
        }
        only_254 = list_images(session=session, team=254, page=1, page_size=60)
        assert [i.foreign_key for i in only_254["items"]] == ["BBBBBBB"]
        pending = list_images(
            session=session, status=ImageStatus.pending, page=1, page_size=60
        )
        assert pending["total"] == 1 and pending["counts"]["removed"] == 1
