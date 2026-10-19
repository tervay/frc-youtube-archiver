from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Optional

import httpx
from sqlmodel import Session, select

from ..db import get_all_settings, get_engine
from ..models import Image, ImageStatus, utcnow
from .image_scanner import IMAGE_URLS

log = logging.getLogger("archiver.images")

_IDLE_POLL = 5.0
_BACKOFF_START = 60.0
_BACKOFF_MAX = 3600.0


class RateLimited(Exception):
    def __init__(self, retry_after: float):
        super().__init__(f"rate limited (retry-after={retry_after})")
        self.retry_after = retry_after


def image_path(media_root: str, image: Image, ext: str) -> Path:
    return (
        Path(media_root)
        / "images"
        / str(image.team_number)
        / str(image.year)
        / f"{image.foreign_key}.{ext}"
    )


def download_next() -> Optional[float]:
    with Session(get_engine()) as session:
        settings = get_all_settings(session)
        if not settings["images_enabled"]:
            return None
        image = session.exec(
            select(Image).where(Image.status == ImageStatus.pending).order_by(Image.id)
        ).first()
        if image is None:
            return None
        if _copy_existing(session, image, settings["media_root"]):
            return 0.0
        _fetch(session, image, settings)
        return settings["image_request_interval"]


def _copy_existing(session: Session, image: Image, media_root: str) -> bool:
    source = session.exec(
        select(Image).where(
            Image.provider == image.provider,
            Image.foreign_key == image.foreign_key,
            Image.status == ImageStatus.downloaded,
        )
    ).first()
    if source is None or not source.file_path or not Path(source.file_path).is_file():
        return False
    target = image_path(media_root, image, source.ext)
    try:
        _write(target, lambda tmp: shutil.copy2(source.file_path, tmp))
    except OSError as e:
        log.warning("could not copy %s -> %s: %s", source.file_path, target, e)
        return False
    _mark_downloaded(session, image, target, source.ext)
    return True


def _fetch(session: Session, image: Image, settings: dict) -> None:
    url = IMAGE_URLS[image.provider].format(image.foreign_key)
    try:
        resp = httpx.get(
            url,
            headers={"User-Agent": settings["image_user_agent"]},
            follow_redirects=False,
            timeout=30.0,
        )
    except httpx.HTTPError as e:
        _fail(session, image, settings, f"{url}: {e}")
        return
    if resp.status_code == 429:
        raise RateLimited(_retry_after(resp))
    if resp.is_redirect or resp.status_code == 404:
        image.status = ImageStatus.removed
        image.error = f"HTTP {resp.status_code} {resp.headers.get('location', '')}"
        session.add(image)
        session.commit()
        log.info("image %s removed upstream (%s)", image.foreign_key, image.error)
        return
    if resp.status_code != 200:
        _fail(session, image, settings, f"{url}: HTTP {resp.status_code}")
        return
    content_type = resp.headers.get("content-type", "").split(";")[0].strip()
    ext = (mimetypes.guess_extension(content_type) or ".jpg").lstrip(".")
    target = image_path(settings["media_root"], image, ext)
    try:
        _write(target, lambda tmp: tmp.write_bytes(resp.content))
    except OSError as e:
        _fail(session, image, settings, f"write {target}: {e}")
        return
    _mark_downloaded(session, image, target, ext)


def _retry_after(resp: httpx.Response) -> float:
    try:
        return max(float(resp.headers.get("retry-after") or 0), 0.0)
    except ValueError:
        return 0.0


def _write(target: Path, produce: Callable[[Path], object]) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".part")
    try:
        produce(tmp)
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)


def _mark_downloaded(session: Session, image: Image, target: Path, ext: str) -> None:
    image.status = ImageStatus.downloaded
    image.file_path = str(target)
    image.ext = ext
    image.size = target.stat().st_size
    image.error = None
    image.downloaded_at = utcnow()
    session.add(image)
    session.commit()
    log.info("downloaded image %s -> %s", image.foreign_key, target)


def _fail(session: Session, image: Image, settings: dict, error: str) -> None:
    image.attempts += 1
    image.error = error
    if image.attempts > int(settings["max_retries"]):
        image.status = ImageStatus.failed
    session.add(image)
    session.commit()
    log.warning(
        "image %s attempt %d failed: %s", image.foreign_key, image.attempts, error
    )


class ImageDownloader:
    def __init__(self) -> None:
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="img")
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            await self._task
        self._executor.shutdown(wait=False, cancel_futures=True)

    async def _loop(self) -> None:
        loop = asyncio.get_running_loop()
        backoff = _BACKOFF_START
        while not self._stop.is_set():
            delay = _IDLE_POLL
            try:
                pause = await loop.run_in_executor(self._executor, download_next)
                if pause is not None:
                    delay = pause
                    backoff = _BACKOFF_START
            except RateLimited as e:
                delay = e.retry_after or backoff
                if not e.retry_after:
                    backoff = min(backoff * 2, _BACKOFF_MAX)
                log.warning(
                    "imgur rate limited us; pausing %.0fs. A Python User-Agent "
                    "gets an instant 429 — check the image_user_agent setting.",
                    delay,
                )
            except Exception:
                log.exception("image download step failed (continuing)")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
