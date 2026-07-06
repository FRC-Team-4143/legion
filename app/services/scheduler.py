"""
APScheduler jobs. Legion currently has a single scheduled job — a rotating nightly
SQLite backup snapshot — mirroring the sibling apps' backup schedule.
"""
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import settings

log = logging.getLogger(__name__)


async def job_nightly_backup() -> None:
    from app.services.backup import is_sqlite, nightly_backup
    if not is_sqlite():
        return
    try:
        nightly_backup()
    except Exception:  # never let a backup failure crash the scheduler
        log.exception("Backup failed")


def register_jobs(scheduler: AsyncIOScheduler) -> None:
    """(Re)register scheduled jobs from current settings. Safe to call on a running
    scheduler (``replace_existing=True``)."""
    bh, bm = settings.backup_time.split(":")
    scheduler.add_job(
        job_nightly_backup,
        CronTrigger(
            day_of_week=settings.backup_day,
            hour=int(bh),
            minute=int(bm),
            timezone=settings.timezone,
        ),
        id="nightly_backup",
        replace_existing=True,
    )


def reschedule_all(scheduler) -> None:
    """Re-apply every job trigger from current settings on a live scheduler. No-op if None."""
    if scheduler is None:
        return
    register_jobs(scheduler)


def create_scheduler() -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=settings.timezone)
    register_jobs(scheduler)
    return scheduler
