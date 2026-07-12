"""Thin Ganymede REST API client (Twitch archiver on the NAS).

Deliberately sync + httpx, mirroring ``tba/client.py``, and always called from a
worker thread (never the event loop).

**Hard safety constraint: this client is strictly additive/read-only.** It
exposes only ``GET`` and two additive ``POST`` helpers — there is no generic
``request(method, ...)`` entry point, and no ``DELETE``/``PUT`` is implemented
anywhere in this module. It must never be extended to remove or disable a
Ganymede channel.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

log = logging.getLogger("archiver.ganymede")


class GanymedeError(RuntimeError):
    pass


class GanymedeClient:
    def __init__(self, base_url: str, api_key: str):
        if not api_key:
            raise GanymedeError("Ganymede API key is not configured.")
        if not base_url:
            raise GanymedeError("Ganymede base URL is not configured.")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}", "accept": "application/json"}

    def _get(self, path: str, params: Optional[dict] = None) -> Any:
        url = f"{self.base_url}{path}"
        log.debug("ganymede client: GET %s params=%s", path, params)
        try:
            resp = httpx.get(url, headers=self._headers(), params=params, timeout=30.0)
        except httpx.HTTPError as e:
            log.warning("ganymede client: GET %s failed: %s", path, e)
            raise GanymedeError(f"Ganymede GET {path} failed: {e}") from e
        log.debug("ganymede client: GET %s -> %d", path, resp.status_code)
        if resp.status_code == 401:
            raise GanymedeError("Ganymede rejected the API key (401).")
        if resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            log.warning(
                "ganymede client: GET %s -> %d: %s",
                path,
                resp.status_code,
                resp.text[:200],
            )
            raise GanymedeError(
                f"Ganymede {resp.status_code} for GET {path}: " f"{resp.text[:200]}"
            )
        return self._unwrap(resp.json())

    def _post(self, path: str, json: dict) -> Any:
        url = f"{self.base_url}{path}"
        log.debug("ganymede client: POST %s body=%s", path, json)
        try:
            resp = httpx.post(url, headers=self._headers(), json=json, timeout=30.0)
        except httpx.HTTPError as e:
            log.warning("ganymede client: POST %s failed: %s", path, e)
            raise GanymedeError(f"Ganymede POST {path} failed: {e}") from e
        log.debug("ganymede client: POST %s -> %d", path, resp.status_code)
        if resp.status_code == 401:
            raise GanymedeError("Ganymede rejected the API key (401).")
        if resp.status_code >= 400:
            log.warning(
                "ganymede client: POST %s -> %d: %s",
                path,
                resp.status_code,
                resp.text[:200],
            )
            raise GanymedeError(
                f"Ganymede {resp.status_code} for POST {path}: " f"{resp.text[:200]}"
            )
        return self._unwrap(resp.json())

    @staticmethod
    def _unwrap(payload: Any) -> Any:
        """Every Ganymede response is wrapped as {"success", "data", "message"}."""
        if isinstance(payload, dict) and "data" in payload:
            return payload["data"]
        return payload

    # --- endpoint helpers (GET/POST only — see module docstring) -----------
    def get_channel_by_name(self, name: str) -> Optional[dict]:
        return self._get(f"/channel/name/{name}")

    def archive_channel(self, name: str) -> dict:
        """Create the channel in Ganymede (and fetch its profile image)."""
        return self._post("/archive/channel", {"channel_name": name})

    def list_watched(self) -> list[dict]:
        """All currently-watched channels — the source of truth for dedup."""
        return self._get("/live") or []

    def add_watched(
        self,
        channel_id: str,
        *,
        resolution: str,
        vod_resolution: str,
        watch_live: bool,
        watch_vod: bool,
        download_archives: bool,
        archive_chat: bool,
        render_chat: bool,
    ) -> dict:
        return self._post(
            "/live",
            {
                "channel_id": channel_id,
                "resolution": resolution,
                "vod_resolution": vod_resolution,
                "watch_live": watch_live,
                "watch_vod": watch_vod,
                "download_archives": download_archives,
                "archive_chat": archive_chat,
                "render_chat": render_chat,
                # Not used (watch_clips is always False), but Ganymede's validator
                # requires clips_limit/clips_interval_days >= 1 regardless — these
                # match the defaults on every channel already tracked via its UI.
                "clips_limit": 5,
                "clips_interval_days": 7,
                "update_metadata_minutes": 15,
            },
        )
