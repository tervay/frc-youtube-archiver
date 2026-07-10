"""Entry points for the scan and reconcile jobs, safe to run in a thread."""
from __future__ import annotations

from sqlmodel import Session

from .. import events
from ..db import get_engine
from ..models import ScanRun
from .reconciler import reconcile
from .scanner import Scanner


def run_scan() -> ScanRun:
    with Session(get_engine()) as session:
        run = Scanner(session).run()
    events.publish("scan_done", {"kind": "scan", "discovered": run.discovered,
                                 "enqueued": run.enqueued, "ok": run.ok,
                                 "message": run.message})
    return run


def run_reconcile() -> ScanRun:
    with Session(get_engine()) as session:
        run = reconcile(session)
    events.publish("scan_done", {"kind": "reconcile", "message": run.message})
    return run
