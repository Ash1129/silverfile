"""Retention: a return nobody is working on does not keep its data forever.

A return that is not complete or halted and has not been touched for
SILVERFILE_RETENTION_DAYS (30 by default) is abandoned. `purge_abandoned`
deletes it entirely through service.delete_return: checkpoints, encrypted
uploads, index row, access code and sessions, model spend. Finished drafts are
kept for the preparer (and deleted by an admin, or by `--include-finished`).
It runs when the dashboard starts and with `python run.py purge`; each
deletion is a security log event.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from . import auth

FINISHED = ("complete", "halted")


def retention_days() -> int:
    return int(os.environ.get("SILVERFILE_RETENTION_DAYS", "30"))


def purge_abandoned(days: int | None = None, *, include_finished: bool = False, now: datetime | None = None) -> list[str]:
    from .. import service

    days = retention_days() if days is None else days
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=days)
    gone: list[str] = []
    for row in service.list_returns():
        try:
            updated = datetime.fromisoformat(row["updated_at"])
        except (TypeError, ValueError):
            continue
        if updated >= cutoff or (row["status"] in FINISHED and not include_finished):
            continue
        service.delete_return(row["return_id"], actor=auth.system("retention"), reason=f"idle more than {days} days ({row['status']})")
        gone.append(row["return_id"])
    return gone
