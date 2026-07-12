"""SQLite engine, schema creation, and the typed settings accessor."""
from __future__ import annotations

import json
import os
from contextlib import contextmanager
from typing import Any, Iterator

from sqlalchemy import text
from sqlmodel import Session, SQLModel, create_engine, select

from . import models  # noqa: F401  (ensure tables are registered)
from .models import Setting
from .paths import DB_PATH, ensure_config_dir
from .settings_defaults import SETTINGS, SETTINGS_BY_KEY

_engine = None


def get_engine():
    global _engine
    if _engine is None:
        ensure_config_dir()
        url = os.environ.get("ARCHIVER_DB_URL", f"sqlite:///{DB_PATH}")
        _engine = create_engine(url, connect_args={"check_same_thread": False})
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
]


def _migrate() -> None:
    engine = get_engine()
    with engine.begin() as conn:
        for table, column, coltype in _ADD_COLUMNS:
            cols = {row[1] for row in
                    conn.execute(text(f"PRAGMA table_info({table})"))}
            if column not in cols:
                conn.execute(
                    text(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}"))


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
