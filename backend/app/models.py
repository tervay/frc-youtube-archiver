"""SQLModel table definitions — the SQLite schema and its enums.

The ``Video`` table is the source of truth for deduplication: one row per
YouTube video, keyed on the stable ``youtube_id``. Nothing about a file's name
or codec is ever used to decide whether to re-download — that all lives on the
row and is refreshed by the reconciler.
"""
from __future__ import annotations

import enum
from datetime import datetime, timezone
from typing import Optional

from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SourceType(str, enum.Enum):
    event_vod = "event_vod"
    match = "match"
    manual = "manual"


class VideoStatus(str, enum.Enum):
    discovered = "discovered"
    queued = "queued"
    downloading = "downloading"
    completed = "completed"
    failed = "failed"
    skipped_live = "skipped_live"


class JobState(str, enum.Enum):
    pending = "pending"
    running = "running"
    done = "done"
    error = "error"
    canceled = "canceled"


class SourceKind(str, enum.Enum):
    season = "season"
    district = "district"
    team = "team"


class Video(SQLModel, table=True):
    __tablename__ = "video"

    id: Optional[int] = Field(default=None, primary_key=True)
    youtube_id: str = Field(index=True, unique=True)
    title: str = ""
    webpage_url: str = ""

    source_type: SourceType = SourceType.event_vod
    event_key: Optional[str] = Field(default=None, index=True)
    match_key: Optional[str] = Field(default=None, index=True)
    year: Optional[int] = Field(default=None, index=True)
    team_keys: str = ""  # comma-separated frcNNN list for match videos

    status: VideoStatus = Field(default=VideoStatus.discovered, index=True)
    force_redownload: bool = False

    # Result of the download (what we originally fetched).
    file_path: Optional[str] = None
    orig_ext: Optional[str] = None
    orig_container: Optional[str] = None
    orig_vcodec: Optional[str] = None
    orig_size: Optional[int] = None
    duration: Optional[int] = None
    downloaded_at: Optional[datetime] = None

    # On-disk state, refreshed by the reconciler.
    present: bool = False
    current_ext: Optional[str] = None
    current_vcodec: Optional[str] = None
    current_size: Optional[int] = None
    transcoded: bool = False
    last_seen_at: Optional[datetime] = None

    error: Optional[str] = None
    retry_count: int = 0

    discovered_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class DownloadJob(SQLModel, table=True):
    __tablename__ = "download_job"

    id: Optional[int] = Field(default=None, primary_key=True)
    video_id: int = Field(foreign_key="video.id", index=True)
    state: JobState = Field(default=JobState.pending, index=True)

    progress_pct: float = 0.0
    speed: Optional[str] = None
    eta: Optional[str] = None
    downloaded_bytes: int = 0
    total_bytes: int = 0

    attempts: int = 0
    worker_id: Optional[str] = None
    log_tail: str = ""

    created_at: datetime = Field(default_factory=utcnow)
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None


class Source(SQLModel, table=True):
    __tablename__ = "source"

    id: Optional[int] = Field(default=None, primary_key=True)
    kind: SourceKind
    value: str = Field(index=True)  # e.g. "2026", "2026ne", "frc254"
    enabled: bool = True
    notes: str = ""
    created_at: datetime = Field(default_factory=utcnow)


class Setting(SQLModel, table=True):
    __tablename__ = "setting"

    key: str = Field(primary_key=True)
    value: str = ""  # JSON-encoded


class TbaCache(SQLModel, table=True):
    __tablename__ = "tba_cache"

    path: str = Field(primary_key=True)
    etag: Optional[str] = None
    last_modified: Optional[str] = None
    body: Optional[str] = None  # cached JSON payload for 304 replay
    fetched_at: datetime = Field(default_factory=utcnow)


class ScanRun(SQLModel, table=True):
    __tablename__ = "scan_run"

    id: Optional[int] = Field(default=None, primary_key=True)
    kind: str = "scan"  # "scan" | "reconcile"
    started_at: datetime = Field(default_factory=utcnow)
    finished_at: Optional[datetime] = None
    discovered: int = 0
    enqueued: int = 0
    errors: int = 0
    ok: bool = True
    message: str = ""
