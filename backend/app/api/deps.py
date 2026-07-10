"""FastAPI dependencies."""
from __future__ import annotations

from typing import Iterator

from sqlmodel import Session

from ..db import get_engine


def get_session() -> Iterator[Session]:
    with Session(get_engine()) as session:
        yield session
