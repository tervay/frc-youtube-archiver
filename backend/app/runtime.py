"""Process-wide handles wired up at startup (the download manager)."""
from __future__ import annotations

from typing import Optional

from .services.worker import DownloadManager

manager: Optional[DownloadManager] = None
