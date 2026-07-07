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


async def job_sync_slack_profiles() -> None:
    """Push member metadata into Slack custom profile fields. No-op when Slack isn't
    configured or automated updates are disabled."""
    if not settings.slack_bot_token or not settings.updates_enabled:
        return
    try:
        from app.database import AsyncSessionLocal
        from app.services.slack_profile import sync_all_profiles
        async with AsyncSessionLocal() as db:
            result = await sync_all_profiles(db, automated=True)
        log.info("Slack profile sync: %s", result)
    except Exception:  # never let a Slack failure crash the scheduler
        log.exception("Slack profile sync failed")


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

    sh, sm = settings.slack_sync_time.split(":")
    scheduler.add_job(
        job_sync_slack_profiles,
        CronTrigger(
            day_of_week=settings.slack_sync_day,
            hour=int(sh),
            minute=int(sm),
            timezone=settings.timezone,
        ),
        id="slack_profile_sync",
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
