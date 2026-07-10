"""Central logging setup: stdout (for ``docker logs``) + a rotating file.

Called once at startup from ``main.lifespan``. The level can be raised to DEBUG
via the ``log_level`` setting (UI) or the ``ARCHIVER_LOG_LEVEL`` env var to get
verbose download diagnostics, including yt-dlp's own extractor/downloader output
(see ``YtdlpLogger`` and ``ytdlp_runner``).
"""
from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler

from .paths import CONFIG_DIR

_FORMAT = "%(asctime)s %(levelname)-7s [%(threadName)s] %(name)s: %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"
_LOG_FILE = CONFIG_DIR / "logs" / "archiver.log"

# Set once we've attached handlers so a second call (e.g. a settings-driven
# re-apply) only adjusts levels instead of stacking duplicate handlers.
_configured = False


def setup_logging(level: str | None = None) -> None:
    """Configure the root logger. Safe to call more than once.

    ``level`` (from the ``log_level`` setting) wins; then ``ARCHIVER_LOG_LEVEL``;
    else INFO. A second call just re-applies the level.
    """
    global _configured
    level_name = (level or os.environ.get("ARCHIVER_LOG_LEVEL") or "INFO").upper()
    lvl = getattr(logging, level_name, logging.INFO)

    root = logging.getLogger()
    root.setLevel(lvl)

    if not _configured:
        fmt = logging.Formatter(_FORMAT, datefmt=_DATEFMT)

        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(fmt)
        root.addHandler(console)

        # Best-effort file handler; keep going on read-only/odd volumes.
        try:
            _LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
            fileh = RotatingFileHandler(
                _LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=5,
                encoding="utf-8")
            fileh.setFormatter(fmt)
            root.addHandler(fileh)
        except OSError as e:  # pragma: no cover - depends on volume perms
            root.warning("File logging disabled (%s): %s", _LOG_FILE, e)

        # Uvicorn/httpx access logs are noisy; keep them at WARNING regardless.
        logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        _configured = True

    root.info("Logging configured at level %s (file: %s)", level_name, _LOG_FILE)


class YtdlpLogger:
    """Adapter so yt-dlp's internal logging flows into our Python logger.

    yt-dlp prefixes debug lines with ``[debug]`` and its normal progress/info
    lines come through ``debug`` too; we route the latter to INFO so they show
    up without enabling DEBUG, and downgrade the ``[debug]`` spam to DEBUG.
    """

    def __init__(self, logger: logging.Logger) -> None:
        self._log = logger

    def debug(self, msg: str) -> None:
        if msg.startswith("[debug] "):
            self._log.debug(msg)
        else:
            self._log.info(msg)

    def info(self, msg: str) -> None:
        self._log.info(msg)

    def warning(self, msg: str) -> None:
        self._log.warning(msg)

    def error(self, msg: str) -> None:
        self._log.error(msg)
