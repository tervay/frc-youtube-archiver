import os
import tempfile

import pytest


@pytest.fixture()
def temp_env(monkeypatch):
    """Point the app at throwaway config/media/db locations."""
    tmp = tempfile.mkdtemp()
    monkeypatch.setenv("ARCHIVER_CONFIG_DIR", os.path.join(tmp, "config"))
    monkeypatch.setenv("ARCHIVER_MEDIA_DIR", os.path.join(tmp, "media"))
    monkeypatch.setenv("ARCHIVER_DB_URL", f"sqlite:///{tmp}/config/archiver.db")
    os.makedirs(os.path.join(tmp, "config"), exist_ok=True)
    os.makedirs(os.path.join(tmp, "media"), exist_ok=True)

    # Reset the cached engine so each test gets a fresh DB.
    import app.db as db
    db._engine = None
    db.init_db()
    yield tmp
    db._engine = None
