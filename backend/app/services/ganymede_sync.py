"""Reconcile TBA Twitch webcasts against Ganymede's watched channels.

Ganymede is the single source of truth for what's tracked — this file keeps no
local mirror of Ganymede state. Each run:

  1. computes the *desired* set of Twitch channel logins from TBA (event
     webcasts of type ``twitch``, on the same in-scope sources/events the
     YouTube scanner uses — but, unlike the YouTube path, **not** gated on the
     event having ended: a Twitch channel is worth tracking whether the event
     is upcoming, live, or long past);
  2. reads the *current* set of watched channels straight from Ganymede
     (``GET /live``);
  3. adds only the channels missing from Ganymede's current set.

This is intentionally add-only: a channel Ganymede already watches (including
ones added manually, or for events this app no longer scans) is left alone.
Nothing is ever deleted or disabled — see ``GanymedeClient``'s module
docstring for the hard constraint that makes that true at the HTTP layer too.
"""

from __future__ import annotations

import logging
from typing import Optional

from sqlmodel import Session, select

from ..models import Source, SourceKind
from ..tba.client import TbaClient
from .ganymede_client import GanymedeClient, GanymedeError
from .scanner import _event_in_scope

log = logging.getLogger("archiver.ganymede")


class GanymedeSyncResult:
    def __init__(self):
        self.desired = 0
        self.added = 0
        self.errors = 0
        self.messages: list[str] = []


class GanymedeSync:
    def __init__(
        self,
        session: Session,
        settings: dict,
        tba_client: Optional[TbaClient] = None,
        gany_client: Optional[GanymedeClient] = None,
    ):
        self.session = session
        self.settings = settings
        self.tba_client = tba_client
        self.gany_client = gany_client

    def run(self) -> GanymedeSyncResult:
        result = GanymedeSyncResult()

        tba = self.tba_client
        if tba is None:
            tba = TbaClient(self.settings["tba_api_key"], self.session)

        gany = self.gany_client
        if gany is None:
            gany = GanymedeClient(
                self.settings["ganymede_base_url"], self.settings["ganymede_api_key"]
            )

        desired = self._desired_logins(tba)
        result.desired = len(desired)
        log.info(
            "ganymede sync: %d desired twitch channel(s) from TBA: %s",
            len(desired),
            ", ".join(sorted(desired)) or "(none)",
        )
        if not desired:
            log.info("ganymede sync: no desired channels, nothing to do")
            return result

        try:
            current = self._current_logins(gany)
        except GanymedeError as e:
            result.errors += 1
            result.messages.append(f"Could not read Ganymede's watched channels: {e}")
            log.warning("ganymede sync: failed to list watched channels: %s", e)
            return result
        log.info(
            "ganymede sync: %d channel(s) already watched by Ganymede: %s",
            len(current),
            ", ".join(sorted(current)) or "(none)",
        )

        missing = sorted(desired - current)
        log.info(
            "ganymede sync: %d channel(s) missing from Ganymede: %s",
            len(missing),
            ", ".join(missing) or "(none)",
        )
        for login in missing:
            try:
                self._track(gany, login)
                result.added += 1
                log.info("ganymede sync: now tracking %s", login)
            except GanymedeError as e:
                result.errors += 1
                result.messages.append(f"{login}: {e}")
                log.warning("ganymede sync: failed to track %s: %s", login, e)

        log.info(
            "ganymede sync: summary — %d desired, %d already watched, "
            "%d sent to ganymede (%d succeeded, %d failed)",
            result.desired,
            len(current),
            len(missing),
            result.added,
            result.errors,
        )
        return result

    # --- desired set (TBA) --------------------------------------------------
    def _desired_logins(self, tba: TbaClient) -> set[str]:
        sources = self.session.exec(
            select(Source).where(Source.enabled == True)  # noqa: E712
        ).all()
        log.info(
            "ganymede sync: %d enabled source(s): %s",
            len(sources),
            ", ".join(f"{s.kind}:{s.value}" for s in sources) or "(none)",
        )

        logins: set[str] = set()
        for src in sources:
            if src.kind == SourceKind.season:
                events = self._season_events(tba, int(src.value))
            elif src.kind == SourceKind.district:
                events = tba.district_events(src.value)
            else:
                # Team sources carry no event webcasts (match objects only) —
                # out of scope for now.
                log.debug(
                    "ganymede sync: source %s:%s is a team source, skipping",
                    src.kind,
                    src.value,
                )
                continue
            log.info(
                "ganymede sync: source %s:%s has %d event(s)",
                src.kind,
                src.value,
                len(events),
            )
            in_scope = 0
            for event in events:
                if src.kind == SourceKind.season and not _event_in_scope(event):
                    log.debug(
                        "ganymede sync: event %s is not in scope, skipping",
                        event.get("key"),
                    )
                    continue
                in_scope += 1
                event_logins = self._twitch_logins(event)
                if event_logins:
                    log.info(
                        "ganymede sync: event %s has twitch webcast(s): %s",
                        event.get("key"),
                        ", ".join(sorted(event_logins)),
                    )
                logins |= event_logins
            log.info(
                "ganymede sync: source %s:%s — %d/%d event(s) in scope",
                src.kind,
                src.value,
                in_scope,
                len(events),
            )
        return logins

    def _season_events(self, tba: TbaClient, year: int) -> list[dict]:
        log.debug("ganymede sync: fetching season %d events from TBA", year)
        events = tba.season_events(year)
        log.debug("ganymede sync: found %d season %d events", len(events), year)
        return events

    @staticmethod
    def _twitch_logins(event: dict) -> set[str]:
        logins: set[str] = set()
        for webcast in event.get("webcasts") or []:
            if webcast.get("type") != "twitch":
                continue
            login = (webcast.get("channel") or "").strip().lower()
            if login:
                logins.add(login)
        return logins

    # --- current set (Ganymede) --------------------------------------------
    @staticmethod
    def _current_logins(gany: GanymedeClient) -> set[str]:
        current: set[str] = set()
        for entry in gany.list_watched():
            channel = (entry.get("edges") or {}).get("channel") or {}
            name = (channel.get("name") or "").strip().lower()
            if name:
                current.add(name)
        return current

    # --- apply ---------------------------------------------------------------
    def _track(self, gany: GanymedeClient, login: str) -> None:
        channel = gany.get_channel_by_name(login)
        if channel is None:
            log.info(
                "ganymede sync: channel %s not known to Ganymede, archiving", login
            )
            channel = gany.archive_channel(login)
        else:
            log.debug(
                "ganymede sync: channel %s already known, id=%s",
                login,
                channel.get("id"),
            )
        gany.add_watched(
            channel["id"],
            resolution=self.settings["ganymede_resolution"],
            vod_resolution=self.settings["ganymede_vod_resolution"],
            watch_live=True,
            watch_vod=self.settings["ganymede_watch_vod"],
            download_archives=self.settings["ganymede_watch_vod"],
            archive_chat=self.settings["ganymede_archive_chat"],
            render_chat=self.settings["ganymede_archive_chat"],
        )
