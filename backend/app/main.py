"""FastAPI application: API + SSE + the built React SPA, one process/port."""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import logging

from sqlmodel import Session

from . import events, runtime, scheduler
from .api import api_router
from .db import get_all_settings, get_engine, init_db
from .logging_config import setup_logging
from .services.worker import DownloadManager, recover_interrupted_jobs

STATIC_DIR = Path(os.environ.get("ARCHIVER_STATIC_DIR", "/app/static"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()  # console + file, so anything below is captured
    init_db()
    with Session(get_engine()) as session:
        setup_logging(get_all_settings(session).get("log_level"))  # honor saved level
    log = logging.getLogger("archiver.startup")
    recovered = (
        recover_interrupted_jobs()
    )  # requeue jobs left mid-download by a restart
    if recovered:
        log.info("Recovered %d interrupted download job(s) -> pending", recovered)
    events.set_loop(asyncio.get_running_loop())
    runtime.manager = DownloadManager()
    runtime.manager.start()
    scheduler.start()
    log.info("Startup complete; download manager and scheduler running")
    try:
        yield
    finally:
        log.info("Shutting down; stopping scheduler and download manager")
        scheduler.shutdown()
        if runtime.manager:
            await runtime.manager.stop()


app = FastAPI(title="FRC Archiver", lifespan=lifespan)
app.include_router(api_router)


@app.get("/api/health")
def health():
    return {"status": "ok"}


# --- Static SPA (mounted last so /api wins) -----------------------------------
if (STATIC_DIR / "assets").exists():
    app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="assets")


@app.get("/{full_path:path}")
def spa(full_path: str):
    """Serve built assets, falling back to index.html for client-side routes."""
    if full_path.startswith("api/"):
        return JSONResponse({"detail": "Not found"}, status_code=404)
    candidate = STATIC_DIR / full_path
    if full_path and candidate.is_file():
        return FileResponse(candidate)
    index = STATIC_DIR / "index.html"
    if index.exists():
        return FileResponse(index)
    return JSONResponse(
        {"detail": "Frontend not built. Run the Vite build or use the Docker image."},
        status_code=200,
    )
