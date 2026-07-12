"""Filesystem locations, overridable via environment for local dev / testing.

In the Docker image these default to the two mounted volumes:
  - /config  -> SQLite db + optional cookies.txt
  - /media   -> downloaded videos (shared with tdarr)
"""

from __future__ import annotations

import os
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("ARCHIVER_CONFIG_DIR", "/config"))
DEFAULT_MEDIA_DIR = os.environ.get("ARCHIVER_MEDIA_DIR", "/media")

DB_PATH = CONFIG_DIR / "archiver.db"


def ensure_config_dir() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
