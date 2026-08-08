"""
APScheduler jobs: a rotating nightly SQLite backup snapshot (mirroring the sibling
apps' backup schedule), a nightly Slack custom-profile sync, and a frequent sweep that
deletes aged SSO Approve/Deny DMs + their AuthRequest rows.
"""
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

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


async def job_purge_challenge_dms() -> None:
    """Delete aged SSO Approve/Deny DMs (and their AuthRequest rows) so they don't pile
    up in members' DM threads with the auth bot. No-op when Slack auth isn't configured."""
    if not settings.slack_auth_bot_token:
        return
    try:
        from app.database import AsyncSessionLocal
        from app.services.slack_auth import purge_old_challenge_dms
        async with AsyncSessionLocal() as db:
            reaped = await purge_old_challenge_dms(db, settings.sso_dm_retention_minutes)
        if reaped:
            log.info("Purged %d old SSO challenge DM(s)", reaped)
    except Exception:  # never let a Slack failure crash the scheduler
        log.exception("SSO challenge DM purge failed")


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

    scheduler.add_job(
        job_purge_challenge_dms,
        IntervalTrigger(minutes=settings.sso_dm_cleanup_interval_minutes),
        id="purge_challenge_dms",
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
