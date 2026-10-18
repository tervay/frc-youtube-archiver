"""SQLite engine, schema creation, and the typed settings accessor."""

from __future__ import annotations

import json
import logging
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from sqlalchemy import event, text
from sqlmodel import Session, SQLModel, create_engine, select

from . import models  # noqa: F401  (ensure tables are registered)
from .models import Setting
from .paths import DB_PATH, ensure_config_dir
from .settings_defaults import SETTINGS, SETTINGS_BY_KEY

log = logging.getLogger("archiver.db")

_engine = None


def get_engine():
    global _engine
    if _engine is None:
        ensure_config_dir()
        url = os.environ.get("ARCHIVER_DB_URL", f"sqlite:///{DB_PATH}")
        _engine = create_engine(url, connect_args={"check_same_thread": False})

        @event.listens_for(_engine, "connect")
        def _set_sqlite_pragma(dbapi_conn, _):
            # WAL mode needs mmap'd shared memory for lock coordination, which
            # Unraid's shfs (FUSE) doesn't support reliably -- connections hang
            # forever instead of erroring. Stick to the default rollback
            # journal; busy_timeout alone (plain file locking) is FUSE-safe.
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.close()

    return _engine


def init_db() -> None:
    SQLModel.metadata.create_all(get_engine())
    _migrate()
    _seed_settings()


# Additive columns not present in the original schema. ``create_all`` only
# creates missing *tables*, never new columns on an existing one, so on an
# already-populated DB we add them ourselves. Idempotent: skips columns that
# already exist. (table, column, SQLite type)
_ADD_COLUMNS = [
    ("video", "orig_height", "INTEGER"),
    ("video", "current_height", "INTEGER"),
    ("download_job", "phase", "TEXT"),
    ("video", "match_keys", "TEXT"),
    ("source", "start_year", "INTEGER"),
    ("source", "end_year", "INTEGER"),
]


def _migrate() -> None:
    engine = get_engine()
    with engine.begin() as conn:
        for table, column, coltype in _ADD_COLUMNS:
            cols = {row[1] for row in conn.execute(text(f"PRAGMA table_info({table})"))}
            if column not in cols:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}"))
    _merge_suffixed_youtube_ids()
    _backfill_team_source_years()
    _reclassify_unavailable_failures()


def _reclassify_unavailable_failures() -> None:
    """Move pre-existing ``failed`` rows for permanently-gone videos to
    ``unavailable``, so "Retry all failed" stops re-attempting them.

    Uses the same message patterns as ``ytdlp_runner.classify_error``.
    Idempotent: only ``failed`` rows are touched, and a second run matches
    nothing once they've moved to ``unavailable``.
    """
    from .services.ytdlp_runner import UNAVAILABLE_PATTERNS

    engine = get_engine()
    with engine.begin() as conn:
        cols = {row[1] for row in conn.execute(text("PRAGMA table_info(video)"))}
        if "error" not in cols:
            return
        clauses = " OR ".join(
            f"lower(error) LIKE :p{i}" for i in range(len(UNAVAILABLE_PATTERNS))
        )
        params = {f"p{i}": f"%{p}%" for i, p in enumerate(UNAVAILABLE_PATTERNS)}
        conn.execute(
            text(
                f"UPDATE video SET status = 'unavailable' "
                f"WHERE status = 'failed' AND ({clauses})"
            ),
            params,
        )


def _backfill_team_source_years() -> None:
    """Give pre-existing team sources the full 1992-to-ongoing range.

    Before team sources carried their own range they were scanned over the union
    of every season source's year plus the current year. An open-ended range is a
    superset of that, so no team loses coverage. Idempotent: only NULLs are set.
    """
    from .services.scanner import FIRST_FRC_YEAR

    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE source SET start_year = :first "
                "WHERE kind = 'team' AND start_year IS NULL"
            ),
            {"first": FIRST_FRC_YEAR},
        )


def _merge_suffixed_youtube_ids() -> None:
    """Collapse rows whose ``youtube_id`` carried a TBA timestamp suffix.

    TBA lists one video on every match it covers, each with the match's offset
    appended (``o3k9QhAfxfo?t=2395``). Those rows escaped the ``youtube_id``
    dedup and were downloaded once per match. Keep one row per bare id, union
    the match keys onto it, and drop the duplicates with their jobs. Idempotent:
    once no suffixed id is left there is nothing to merge.
    """
    from .services.scanner import normalize_youtube_id

    engine = get_engine()
    with engine.begin() as conn:
        rows = list(
            conn.execute(
                text(
                    "SELECT id, youtube_id, match_key, match_keys, status, "
                    "file_path FROM video ORDER BY id"
                )
            )
        )
        groups: dict[str, list] = {}
        for row in rows:
            groups.setdefault(normalize_youtube_id(row[1]), []).append(row)

        for base, group in groups.items():
            if len(group) == 1 and group[0][1] == base:
                continue
            keeper = next(
                (
                    r
                    for r in group
                    if r[4] == "completed" and r[5] and Path(r[5]).exists()
                ),
                group[0],
            )
            keys: list[str] = []
            for row in group:
                for key in (row[3] or "").split(",") + [row[2] or ""]:
                    if key and key not in keys:
                        keys.append(key)
            losers = [r[0] for r in group if r[0] != keeper[0]]
            if losers:
                params = {f"i{n}": vid for n, vid in enumerate(losers)}
                placeholders = ",".join(f":{k}" for k in params)
                conn.execute(
                    text(
                        f"DELETE FROM download_job WHERE video_id IN ({placeholders})"
                    ),
                    params,
                )
                conn.execute(
                    text(f"DELETE FROM video WHERE id IN ({placeholders})"), params
                )
            conn.execute(
                text(
                    "UPDATE video SET youtube_id = :base, match_keys = :keys "
                    "WHERE id = :id"
                ),
                {"base": base, "keys": ",".join(keys), "id": keeper[0]},
            )
            log.info(
                "merged %d duplicate row(s) into youtube_id=%s (%d match keys)",
                len(losers),
                base,
                len(keys),
            )

        conn.execute(
            text(
                "UPDATE video SET match_keys = match_key "
                "WHERE COALESCE(match_keys, '') = '' AND match_key IS NOT NULL"
            )
        )
        # Rows whose match_key was also NULL (e.g. event_vod videos, which never
        # set one) fell through the backfill above and stayed NULL, crashing
        # `_upsert`'s `video.match_keys.split(",")` the first time a later scan
        # merged a real match_key onto them.
        conn.execute(text("UPDATE video SET match_keys = '' WHERE match_keys IS NULL"))


@contextmanager
def session_scope() -> Iterator[Session]:
    with Session(get_engine()) as session:
        yield session


def _seed_settings() -> None:
    """Insert any missing settings, seeding from env vars where specified."""
    with session_scope() as s:
        existing = {row.key for row in s.exec(select(Setting)).all()}
        for spec in SETTINGS:
            if spec.key in existing:
                continue
            value = spec.default
            if spec.env and os.environ.get(spec.env):
                value = os.environ[spec.env]
            s.add(Setting(key=spec.key, value=json.dumps(value)))
        s.commit()


def _coerce(spec, raw: Any) -> Any:
    if spec.type == "int":
        return int(raw)
    if spec.type == "float":
        return float(raw)
    if spec.type == "bool":
        return bool(raw)
    return str(raw)


def get_setting(session: Session, key: str) -> Any:
    spec = SETTINGS_BY_KEY[key]
    row = session.get(Setting, key)
    if row is None:
        return spec.default
    try:
        return _coerce(spec, json.loads(row.value))
    except (json.JSONDecodeError, ValueError, TypeError):
        return spec.default


def get_all_settings(session: Session) -> dict[str, Any]:
    return {spec.key: get_setting(session, spec.key) for spec in SETTINGS}


def set_setting(session: Session, key: str, value: Any) -> None:
    if key not in SETTINGS_BY_KEY:
        raise KeyError(key)
    coerced = _coerce(SETTINGS_BY_KEY[key], value)
    row = session.get(Setting, key)
    if row is None:
        session.add(Setting(key=key, value=json.dumps(coerced)))
    else:
        row.value = json.dumps(coerced)
        session.add(row)
