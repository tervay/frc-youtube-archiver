"""APScheduler wiring for the daily scan and periodic reconcile.

Jobs run in a thread pool (they do blocking I/O), and their cron expressions are
read from settings. ``reschedule`` is called after settings are saved so changes
take effect without a restart.
"""
from __future__ import annotations

from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlmodel import Session

from .db import get_engine, get_setting
from .services.jobs import run_reconcile, run_scan

_scheduler: Optional[AsyncIOScheduler] = None


def _cron(expr: str) -> CronTrigger:
    minute, hour, dom, month, dow = expr.split()
    return CronTrigger(minute=minute, hour=hour, day=dom, month=month,
                       day_of_week=dow)


def start() -> AsyncIOScheduler:
    global _scheduler
    _scheduler = AsyncIOScheduler()
    _scheduler.start()
    reschedule()
    return _scheduler


def reschedule() -> None:
    if _scheduler is None:
        return
    with Session(get_engine()) as session:
        scan_cron = get_setting(session, "scan_cron")
        reconcile_cron = get_setting(session, "reconcile_cron")
    _scheduler.add_job(run_scan, _cron(scan_cron), id="scan",
                       replace_existing=True, misfire_grace_time=3600)
    _scheduler.add_job(run_reconcile, _cron(reconcile_cron), id="reconcile",
                       replace_existing=True, misfire_grace_time=3600)


def next_run_times() -> dict[str, Optional[str]]:
    if _scheduler is None:
        return {"scan": None, "reconcile": None}
    out = {}
    for jid in ("scan", "reconcile"):
        job = _scheduler.get_job(jid)
        out[jid] = job.next_run_time.isoformat() if job and job.next_run_time else None
    return out


def shutdown() -> None:
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
