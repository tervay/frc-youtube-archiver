"""Thin TheBlueAlliance API v3 client with ETag/304 caching.

Every GET sends ``If-None-Match``/``If-Modified-Since`` from the ``tba_cache``
table; on a 304 we replay the cached JSON body so a daily poll of unchanged
endpoints costs almost nothing. The client is deliberately sync + httpx and is
always called from a worker thread (never the event loop).
"""

from __future__ import annotations

from typing import Any, Optional

import httpx
from sqlmodel import Session

from ..models import TbaCache, utcnow

BASE_URL = "https://www.thebluealliance.com/api/v3"


class TbaError(RuntimeError):
    pass


class TbaClient:
    def __init__(self, api_key: str, session: Session, base_url: str = BASE_URL):
        if not api_key:
            raise TbaError("TBA API key is not configured.")
        self.api_key = api_key
        self.session = session
        self.base_url = base_url.rstrip("/")

    def _get(self, path: str) -> Any:
        cache = self.session.get(TbaCache, path)
        headers = {"X-TBA-Auth-Key": self.api_key, "accept": "application/json"}
        if cache and cache.etag:
            headers["If-None-Match"] = cache.etag
        if cache and cache.last_modified:
            headers["If-Modified-Since"] = cache.last_modified

        url = f"{self.base_url}{path}"
        resp = httpx.get(url, headers=headers, timeout=30.0)

        if resp.status_code == 304 and cache and cache.body is not None:
            return httpx.Response(200, content=cache.body).json()
        if resp.status_code == 401:
            raise TbaError("TBA rejected the API key (401).")
        if resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            raise TbaError(f"TBA {resp.status_code} for {path}: {resp.text[:200]}")

        body_text = resp.text
        self._store_cache(
            path, resp.headers.get("ETag"), resp.headers.get("Last-Modified"), body_text
        )
        return resp.json()

    def _store_cache(
        self, path: str, etag: Optional[str], last_modified: Optional[str], body: str
    ) -> None:
        row = self.session.get(TbaCache, path)
        if row is None:
            row = TbaCache(path=path)
        row.etag = etag
        row.last_modified = last_modified
        row.body = body
        row.fetched_at = utcnow()
        self.session.add(row)
        self.session.commit()

    # --- endpoint helpers -------------------------------------------------
    def season_events(self, year: int) -> list[dict]:
        return self._get(f"/events/{year}") or []

    def district_events(self, district_key: str) -> list[dict]:
        return self._get(f"/district/{district_key}/events") or []

    def team_matches(self, team_key: str, year: int) -> list[dict]:
        return self._get(f"/team/{team_key}/matches/{year}") or []

    def team_events(self, team_key: str, year: int) -> list[dict]:
        return self._get(f"/team/{team_key}/events/{year}") or []
