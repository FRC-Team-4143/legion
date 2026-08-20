"""
APScheduler jobs: a rotating nightly SQLite backup snapshot (mirroring the sibling
apps' backup schedule), a nightly Slack custom-profile sync, a frequent sweep that
deletes aged SSO Approve/Deny DMs + their AuthRequest rows, and a yearly member tenure
increment.
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


async def job_yearly_tenure_increment() -> None:
    """Bump years_on_team by 1 for every member active as of January 31 (this job runs
    just after midnight on Feb 1, so "active right now" == "active on Jan 31" for all
    practical purposes — there's no historical activity snapshot to check against)."""
    try:
        from sqlalchemy import update

        from app.database import AsyncSessionLocal
        from app.models import Member
        from app.services import audit

        async with AsyncSessionLocal() as db:
            result = await db.execute(
                update(Member).where(Member.is_active.is_(True))
                .values(years_on_team=Member.years_on_team + 1)
            )
            if result.rowcount:
                await audit.record(
                    db, None, "member.yearly_tenure_increment",
                    f"Yearly tenure increment: {result.rowcount} active member(s) +1 year",
                    detail={"count": result.rowcount},
                )
            await db.commit()
            if result.rowcount:
                log.info("Yearly tenure increment: %d member(s) bumped", result.rowcount)
    except Exception:  # never let this crash the scheduler
        log.exception("Yearly tenure increment failed")


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

    scheduler.add_job(
        job_yearly_tenure_increment,
        CronTrigger(month=2, day=1, hour=0, minute=5, timezone=settings.timezone),
        id="yearly_tenure_increment",
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
