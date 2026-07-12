"""Discovery scan: expand configured sources into queued Video rows.

Two kinds of YouTube video are discovered:
  * Event livestream VODs from ``Event.webcasts`` (type ``youtube``), scoped by
    the ``season``/``district`` sources and gated on the event having ended.
    Season sources are further filtered to New England/New York events or
    Championship events (see ``_event_in_scope``); district/team sources are
    unaffected.
  * Match videos from ``Team.matches[].videos`` (type ``youtube``), for each
    ``team`` source.

Deduplication is by ``youtube_id``: a video already completed/queued/downloading
is left alone. ``skipped_live`` rows are re-queued so streams that were still in
progress last time get another chance once they've become real VODs.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

from sqlmodel import Session, select

from ..db import get_setting
from ..models import (ScanRun, Source, SourceKind, SourceType, Video,
                      VideoStatus, utcnow)
from ..tba.client import TbaClient, TbaError
from .queue import enqueue_video

YOUTUBE_WATCH = "https://www.youtube.com/watch?v={}"


def _event_has_ended(end_date: Optional[str], buffer_days: int,
                     today: Optional[date] = None) -> bool:
    """True if the event ended more than ``buffer_days`` ago."""
    if not end_date:
        return False
    today = today or date.today()
    try:
        end = date.fromisoformat(end_date)
    except ValueError:
        return False
    return (today - end).days > buffer_days


# New England + New York.
ALLOWED_STATES = {
    "connecticut", "ct", "maine", "me", "massachusetts", "ma",
    "new hampshire", "nh", "rhode island", "ri", "vermont", "vt",
    "new york", "ny",
}
# TBA event_type ints: 3 = Championship Division, 4 = Championship Finals,
# 6 = Festival of Champions.
CHAMPIONSHIP_EVENT_TYPES = {3, 4, 6}


def _event_in_scope(event: dict) -> bool:
    """True if a season-scan event is a New England/New York or Championship event."""
    state = (event.get("state_prov") or "").strip().lower()
    if state in ALLOWED_STATES:
        return True
    return event.get("event_type") in CHAMPIONSHIP_EVENT_TYPES


class Scanner:
    def __init__(self, session: Session, client: Optional[TbaClient] = None):
        self.session = session
        self.client = client
        self.discovered = 0
        self.enqueued = 0

    def run(self) -> ScanRun:
        run = ScanRun(kind="scan")
        try:
            api_key = get_setting(self.session, "tba_api_key")
            client = self.client or TbaClient(api_key, self.session)
            buffer_days = get_setting(self.session, "live_buffer_days")

            sources = self.session.exec(
                select(Source).where(Source.enabled == True)  # noqa: E712
            ).all()
            season_years = {int(s.value) for s in sources
                            if s.kind == SourceKind.season}
            team_years = season_years | {date.today().year}

            for src in sources:
                if src.kind == SourceKind.season:
                    self._scan_season(client, int(src.value), buffer_days)
                elif src.kind == SourceKind.district:
                    self._scan_district(client, src.value, buffer_days)
                elif src.kind == SourceKind.team:
                    self._scan_team(client, src.value, sorted(team_years))

            run.ok = True
        except TbaError as e:
            run.ok = False
            run.message = str(e)
            run.errors = 1
        finally:
            run.discovered = self.discovered
            run.enqueued = self.enqueued
            run.finished_at = utcnow()
            self.session.add(run)
            self.session.commit()
            self.session.refresh(run)
        return run

    # --- source handlers --------------------------------------------------
    def _scan_season(self, client: TbaClient, year: int, buffer_days: int) -> None:
        for event_key in client.season_event_keys(year):
            event = client.event(event_key)
            if event and _event_in_scope(event):
                self._ingest_event_vods(event, buffer_days)

    def _scan_district(self, client: TbaClient, district_key: str,
                       buffer_days: int) -> None:
        for event in client.district_events(district_key):
            self._ingest_event_vods(event, buffer_days)

    def _scan_team(self, client: TbaClient, team_key: str,
                   years: list[int]) -> None:
        for year in years:
            for match in client.team_matches(team_key, year):
                self._ingest_match_videos(match, team_key, year)

    # --- ingestion --------------------------------------------------------
    def _ingest_event_vods(self, event: dict, buffer_days: int) -> None:
        if not _event_has_ended(event.get("end_date"), buffer_days):
            return
        for webcast in event.get("webcasts") or []:
            if webcast.get("type") != "youtube":
                continue
            vid = webcast.get("channel")
            if not vid:
                continue
            self._upsert(
                youtube_id=vid,
                title=f"{event.get('name', event.get('key'))} — livestream",
                source_type=SourceType.event_vod,
                event_key=event.get("key"),
                year=event.get("year"),
            )

    def _ingest_match_videos(self, match: dict, team_key: str, year: int) -> None:
        for video in match.get("videos") or []:
            if video.get("type") != "youtube":
                continue
            vid = video.get("key")
            if not vid:
                continue
            self._upsert(
                youtube_id=vid,
                title=match.get("key", ""),
                source_type=SourceType.match,
                event_key=match.get("event_key"),
                match_key=match.get("key"),
                year=year,
                team_key=team_key,
            )

    def _upsert(self, youtube_id: str, title: str, source_type: SourceType,
                event_key: Optional[str] = None, match_key: Optional[str] = None,
                year: Optional[int] = None, team_key: Optional[str] = None) -> None:
        video = self.session.exec(
            select(Video).where(Video.youtube_id == youtube_id)
        ).first()

        if video is None:
            video = Video(
                youtube_id=youtube_id,
                title=title,
                webpage_url=YOUTUBE_WATCH.format(youtube_id),
                source_type=source_type,
                event_key=event_key,
                match_key=match_key,
                year=year,
                team_keys=team_key or "",
                status=VideoStatus.discovered,
            )
            self.session.add(video)
            self.session.commit()
            self.session.refresh(video)
            self.discovered += 1
            if enqueue_video(self.session, video):
                self.enqueued += 1
            return

        # Existing row: merge team association for match videos.
        if team_key and team_key not in video.team_keys.split(","):
            keys = [k for k in video.team_keys.split(",") if k]
            keys.append(team_key)
            video.team_keys = ",".join(keys)
            self.session.add(video)
            self.session.commit()

        # Dedup: only (re)queue when forced or previously skipped-as-live.
        if video.force_redownload or video.status == VideoStatus.skipped_live:
            video.force_redownload = False
            self.session.add(video)
            self.session.commit()
            if enqueue_video(self.session, video):
                self.enqueued += 1
