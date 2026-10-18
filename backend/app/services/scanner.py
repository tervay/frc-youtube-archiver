"""Discovery scan: expand configured sources into queued Video rows.

Two kinds of YouTube video are discovered:
  * Event livestream VODs from ``Event.webcasts`` (type ``youtube``), scoped by
    the ``season``/``district`` sources and gated on the event having ended.
    Season sources are further filtered to New England/New York events or
    Championship events (see ``_event_in_scope``); district/team sources are
    unaffected.
  * Match videos from ``Event.matches[].videos`` (type ``youtube``): for every
    in-scope event of a ``season`` source (alongside its livestream VOD), for
    every event of a configured ``event_type`` (across all seasons), and — via
    ``Team.matches[].videos`` — for each ``team`` source, over that source's
    own ``start_year``/``end_year`` range (null end = ongoing), intersected
    with the years TBA says the team actually competed.

Deduplication is by ``youtube_id``: a video already completed/queued/downloading
is left alone. ``skipped_live`` rows are re-queued so streams that were still in
progress last time get another chance once they've become real VODs. When a
newly-scanned match/team association points at a video that's already
completed under a *different* source, ``ensure_team_copies`` (``worker.py``)
copies the existing file into that team's own folder rather than re-downloading
it.
"""

from __future__ import annotations

import logging
import time
from datetime import date, timedelta
from typing import Optional

from sqlmodel import Session, select

from ..db import get_setting
from ..models import ScanRun, Source, SourceKind, SourceType, Video, VideoStatus, utcnow
from ..tba.client import TbaClient, TbaError
from .queue import enqueue_video
from .worker import ensure_match_copies, ensure_team_copies

log = logging.getLogger("archiver.scanner")

YOUTUBE_WATCH = "https://www.youtube.com/watch?v={}"
FIRST_FRC_YEAR = 1992


def _source_years(src: Source) -> list[int]:
    """The years a source covers, newest first. A null ``end_year`` means ongoing."""
    now = date.today().year
    start = max(src.start_year or FIRST_FRC_YEAR, FIRST_FRC_YEAR)
    end = min(src.end_year or now, now)
    return list(range(end, start - 1, -1))


def _newest_first(src: Source) -> int:
    if src.kind in (SourceKind.season, SourceKind.district):
        return -int(src.value[:4])
    return 0


def normalize_youtube_id(key: str) -> str:
    """Strip TBA's timestamp/query suffix down to the bare 11-char video id.

    TBA lists the same video on every match it covers, each entry pointing at
    that match's offset (``o3k9QhAfxfo?t=2395``). Keying dedup on the raw string
    downloads one identical copy per match.
    """
    for sep in "?&#":
        key = key.split(sep, 1)[0]
    return key


def _event_has_ended(
    end_date: Optional[str], buffer_days: int, today: Optional[date] = None
) -> bool:
    """True if the event ended at least ``buffer_days`` ago."""
    if not end_date:
        return False
    today = today or date.today()
    try:
        end = date.fromisoformat(end_date)
    except ValueError:
        return False
    return (today - end).days >= buffer_days


# New England + New York.
ALLOWED_STATES = {
    "connecticut",
    "ct",
    "maine",
    "me",
    "massachusetts",
    "ma",
    "new hampshire",
    "nh",
    "rhode island",
    "ri",
    "vermont",
    "vt",
    "new york",
    "ny",
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
        log.info("scan starting")
        scan_started = time.monotonic()
        try:
            api_key = get_setting(self.session, "tba_api_key")
            client = self.client or TbaClient(api_key, self.session)
            buffer_days = get_setting(self.session, "live_buffer_days")
            log.debug("live_buffer_days=%s", buffer_days)

            sources = self.session.exec(
                select(Source).where(Source.enabled == True)  # noqa: E712
            ).all()
            log.info("scanning %d enabled source(s)", len(sources))

            for i, src in enumerate(sorted(sources, key=_newest_first), 1):
                log.info(
                    "source %d/%d: %s %s", i, len(sources), src.kind.value, src.value
                )
                started = time.monotonic()
                discovered, enqueued = self.discovered, self.enqueued
                if src.kind == SourceKind.season:
                    self._scan_season(client, int(src.value), buffer_days)
                elif src.kind == SourceKind.district:
                    self._scan_district(client, src.value, buffer_days)
                elif src.kind == SourceKind.team:
                    self._scan_team(client, src.value, _source_years(src))
                elif src.kind == SourceKind.event_type:
                    self._scan_event_type(client, src.value, buffer_days)
                log.info(
                    "source %s %s done in %.1fs: discovered=%d enqueued=%d",
                    src.kind.value,
                    src.value,
                    time.monotonic() - started,
                    self.discovered - discovered,
                    self.enqueued - enqueued,
                )

            run.ok = True
        except TbaError as e:
            log.error("scan aborted: %s", e)
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
            log.info(
                "scan finished in %.1fs: ok=%s discovered=%d enqueued=%d",
                time.monotonic() - scan_started,
                run.ok,
                run.discovered,
                run.enqueued,
            )
        return run

    # --- source handlers --------------------------------------------------
    def _scan_season(self, client: TbaClient, year: int, buffer_days: int) -> None:
        events = client.season_events(year)
        in_scope = [e for e in events if _event_in_scope(e)]
        log.info(
            "season %d: %d event(s), %d in scope", year, len(events), len(in_scope)
        )
        for event in in_scope:
            self._ingest_event_vods(event, buffer_days)
            self._ingest_event_matches(client, event, buffer_days)

    def _scan_district(
        self, client: TbaClient, district_key: str, buffer_days: int
    ) -> None:
        events = client.district_events(district_key)
        log.info("district %s: %d event(s)", district_key, len(events))
        for event in events:
            self._ingest_event_vods(event, buffer_days)

    def _scan_team(self, client: TbaClient, team_key: str, years: list[int]) -> None:
        played = set(client.years_participated(team_key))
        if played:
            years = [y for y in years if y in played]
        log.info("team %s: %d year(s) to scan %s", team_key, len(years), years)
        for year in years:
            matches = client.team_matches(team_key, year)
            log.info("team %s %d: %d match(es)", team_key, year, len(matches))
            for match in matches:
                self._ingest_match_videos(match, team_key, year)

    def _scan_event_type(self, client: TbaClient, value: str, buffer_days: int) -> None:
        types = {int(t) for t in value.split(",") if t}
        for year in range(date.today().year, FIRST_FRC_YEAR - 1, -1):
            events = [
                e for e in client.season_events(year) if e.get("event_type") in types
            ]
            log.info("event_type %s %d: %d matching event(s)", value, year, len(events))
            for event in events:
                self._ingest_event_matches(client, event, buffer_days)

    # --- ingestion --------------------------------------------------------
    def _ingest_event_matches(
        self, client: TbaClient, event: dict, buffer_days: int
    ) -> None:
        if not _event_has_ended(event.get("end_date"), buffer_days):
            log.debug("event %s: not yet ended, skipping matches", event.get("key"))
            return
        matches = client.event_matches(event.get("key"))
        log.debug("event %s: %d match(es) to check", event.get("key"), len(matches))
        for match in matches:
            self._ingest_match_videos(
                match, None, event.get("year"), SourceType.event_match
            )

    def _ingest_event_vods(self, event: dict, buffer_days: int) -> None:
        event_key = event.get("key")
        if not _event_has_ended(event.get("end_date"), buffer_days):
            log.debug("event %s: not yet ended, skipping", event_key)
            return
        webcasts = event.get("webcasts") or []
        log.debug("event %s: %d webcast(s)", event_key, len(webcasts))
        for webcast in webcasts:
            if webcast.get("type") != "youtube":
                continue
            vid = normalize_youtube_id(webcast.get("channel") or "")
            if not vid:
                continue
            log.debug("event %s: found webcast youtube_id=%s", event_key, vid)
            self._upsert(
                youtube_id=vid,
                title=f"{event.get('name', event.get('key'))} — livestream",
                source_type=SourceType.event_vod,
                event_key=event_key,
                year=event.get("year"),
            )

    def _ingest_match_videos(
        self,
        match: dict,
        team_key: Optional[str],
        year: int,
        source_type: SourceType = SourceType.match,
    ) -> None:
        match_key = match.get("key")
        for video in match.get("videos") or []:
            if video.get("type") != "youtube":
                continue
            vid = normalize_youtube_id(video.get("key") or "")
            if not vid:
                continue
            log.debug(
                "match %s (team=%s): found video youtube_id=%s",
                match_key,
                team_key,
                vid,
            )
            self._upsert(
                youtube_id=vid,
                title=match_key or "",
                source_type=source_type,
                event_key=match.get("event_key"),
                match_key=match_key,
                year=year,
                team_key=team_key,
            )

    def _upsert(
        self,
        youtube_id: str,
        title: str,
        source_type: SourceType,
        event_key: Optional[str] = None,
        match_key: Optional[str] = None,
        year: Optional[int] = None,
        team_key: Optional[str] = None,
    ) -> None:
        video = self.session.exec(
            select(Video).where(Video.youtube_id == youtube_id)
        ).first()

        if video is None:
            log.info("discovered new video youtube_id=%s title=%r", youtube_id, title)
            video = Video(
                youtube_id=youtube_id,
                title=title,
                webpage_url=YOUTUBE_WATCH.format(youtube_id),
                source_type=source_type,
                event_key=event_key,
                match_key=match_key,
                year=year,
                team_keys=team_key or "",
                match_keys=match_key or "",
                status=VideoStatus.discovered,
            )
            self.session.add(video)
            self.session.commit()
            self.session.refresh(video)
            self.discovered += 1
            if enqueue_video(self.session, video):
                self.enqueued += 1
                log.debug("enqueued youtube_id=%s", youtube_id)
            return

        # Existing row: merge the match this video also covers, and copy the
        # already-downloaded file into place for it rather than re-fetching.
        if match_key and match_key not in video.match_keys.split(","):
            log.debug(
                "youtube_id=%s: adding match association match=%s",
                youtube_id,
                match_key,
            )
            keys = [k for k in video.match_keys.split(",") if k]
            keys.append(match_key)
            video.match_keys = ",".join(keys)
            self.session.add(video)
            self.session.commit()
            if video.status == VideoStatus.completed:
                ensure_match_copies(get_setting(self.session, "media_root"), video)

        # Merge team association for match videos.
        if team_key and team_key not in video.team_keys.split(","):
            log.debug(
                "youtube_id=%s: adding team association team=%s", youtube_id, team_key
            )
            keys = [k for k in video.team_keys.split(",") if k]
            keys.append(team_key)
            video.team_keys = ",".join(keys)
            self.session.add(video)
            self.session.commit()
            if video.status == VideoStatus.completed:
                ensure_team_copies(get_setting(self.session, "media_root"), video)

        # Dedup: only (re)queue when forced or previously skipped-as-live.
        if video.force_redownload or video.status == VideoStatus.skipped_live:
            log.info(
                "re-queueing youtube_id=%s (force_redownload=%s, status=%s)",
                youtube_id,
                video.force_redownload,
                video.status,
            )
            video.force_redownload = False
            self.session.add(video)
            self.session.commit()
            if enqueue_video(self.session, video):
                self.enqueued += 1
                log.debug("enqueued youtube_id=%s", youtube_id)
        else:
            log.debug(
                "youtube_id=%s: already known (status=%s), no action",
                youtube_id,
                video.status,
            )
