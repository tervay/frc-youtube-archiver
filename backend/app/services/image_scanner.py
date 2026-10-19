from __future__ import annotations

import logging
import threading
import time
from datetime import date
from typing import Optional

from sqlmodel import Session, select

from ..db import get_setting
from ..models import Image, ScanRun, utcnow
from ..tba.client import TbaClient, TbaError
from .scanner import FIRST_FRC_YEAR

log = logging.getLogger("archiver.images")

IMAGE_URLS = {"imgur": "https://i.imgur.com/{}.jpg"}

_lock = threading.Lock()


class ImageScanner:
    def __init__(self, session: Session, client: Optional[TbaClient] = None):
        self.session = session
        self.client = client
        self.discovered = 0

    def run(self) -> Optional[ScanRun]:
        if not _lock.acquire(blocking=False):
            log.info("image scan already running, skipping")
            return None
        try:
            return self._run()
        finally:
            _lock.release()

    def _run(self) -> ScanRun:
        run = ScanRun(kind="image_scan")
        started = time.monotonic()
        log.info("image scan starting")
        try:
            client = self.client or TbaClient(
                get_setting(self.session, "tba_api_key"), self.session
            )
            recheck_days = get_setting(self.session, "image_recheck_days")
            current = date.today().year
            for year in range(current, FIRST_FRC_YEAR - 1, -1):
                max_age = None if year == current else recheck_days
                events = client.event_keys(year)
                before = self.discovered
                for event_key in events:
                    for media in client.event_team_media(event_key, max_age):
                        for team_key in media.get("team_keys") or []:
                            self._add(media, team_key, year)
                    self.session.commit()
                log.info(
                    "images %d: %d event(s), %d new image(s)",
                    year,
                    len(events),
                    self.discovered - before,
                )
            run.ok = True
            run.message = f"Discovered {self.discovered} new image(s)"
        except TbaError as e:
            log.error("image scan aborted: %s", e)
            run.ok = False
            run.errors = 1
            run.message = str(e)
        finally:
            run.discovered = self.discovered
            run.enqueued = self.discovered
            run.finished_at = utcnow()
            self.session.add(run)
            self.session.commit()
            self.session.refresh(run)
            log.info(
                "image scan finished in %.1fs: ok=%s discovered=%d",
                time.monotonic() - started,
                run.ok,
                run.discovered,
            )
        return run

    def _add(self, media: dict, team_key: str, year: int) -> None:
        provider = media.get("type")
        foreign_key = media.get("foreign_key")
        if provider not in IMAGE_URLS or not foreign_key:
            return
        team_number = int(team_key.removeprefix("frc"))
        existing = self.session.exec(
            select(Image.id).where(
                Image.provider == provider,
                Image.foreign_key == foreign_key,
                Image.team_number == team_number,
                Image.year == year,
            )
        ).first()
        if existing is not None:
            return
        log.debug(
            "discovered %s image %s for %s %d", provider, foreign_key, team_key, year
        )
        self.session.add(
            Image(
                provider=provider,
                foreign_key=foreign_key,
                team_number=team_number,
                year=year,
                preferred=bool(media.get("preferred")),
            )
        )
        self.discovered += 1
