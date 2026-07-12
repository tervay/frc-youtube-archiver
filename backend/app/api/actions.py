"""Manual triggers and scan-run history."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends
from sqlmodel import Session, select

from ..models import ScanRun
from ..services.jobs import run_reconcile, run_resolution_audit, run_scan
from .. import scheduler
from .deps import get_session

router = APIRouter(tags=["actions"])


@router.post("/actions/scan")
async def scan_now():
    run = await asyncio.get_event_loop().run_in_executor(None, run_scan)
    return {"ok": run.ok, "discovered": run.discovered,
            "enqueued": run.enqueued, "message": run.message}


@router.post("/actions/reconcile")
async def reconcile_now():
    run = await asyncio.get_event_loop().run_in_executor(None, run_reconcile)
    return {"message": run.message}


@router.post("/actions/resolution-audit")
async def resolution_audit():
    # Fire-and-forget: probing every completed video via yt-dlp takes many
    # minutes, so we don't await it. Results land in the scan-run history and a
    # "scan_done" SSE event; requeued videos show up in the queue as they're found.
    asyncio.get_event_loop().run_in_executor(None, run_resolution_audit)
    return {"started": True}


@router.get("/actions/schedule")
def schedule():
    return scheduler.next_run_times()


@router.get("/runs")
def list_runs(session: Session = Depends(get_session), limit: int = 50):
    return session.exec(
        select(ScanRun).order_by(ScanRun.started_at.desc()).limit(limit)
    ).all()
