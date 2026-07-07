"""
SSO login rate limiting — caps how often the Slack Approve/Deny prompt can be
triggered, per browser (device cookie) and per matched member, with an
exponentially growing lockout on repeat abuse (e.g. someone spamming a teammate
with push prompts). See `models.AuthThrottle` for the storage shape.
"""
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import AuthThrottle


async def _check_key(db: AsyncSession, key: str) -> Optional[int]:
    """Record one attempt against `key`. Returns None if allowed, else seconds
    remaining on the lockout."""
    now = datetime.utcnow()
    row = (await db.execute(select(AuthThrottle).where(AuthThrottle.key == key))).scalars().first()
    if row is None:
        db.add(AuthThrottle(key=key, attempt_count=1, window_start=now))
        return None

    if row.locked_until and now < row.locked_until:
        return int((row.locked_until - now).total_seconds())

    if (now - row.window_start).total_seconds() > settings.sso_rate_window:
        # The window lapsed clean — reset the count, but keep `lock_count` (the
        # backoff memory) so a repeat offender still escalates faster next time.
        row.window_start = now
        row.attempt_count = 1
        row.locked_until = None
        return None

    row.attempt_count += 1
    if row.attempt_count > settings.sso_rate_max:
        row.lock_count += 1
        backoff = settings.sso_backoff_base * (settings.sso_backoff_multiplier ** (row.lock_count - 1))
        row.locked_until = now + timedelta(seconds=backoff)
        row.window_start = now
        row.attempt_count = 0
        return int(backoff)
    return None


async def check_and_record(db: AsyncSession, device_id: str, member_id: Optional[int]) -> Optional[int]:
    """Check + record one SSO attempt. Returns None if allowed, else seconds to wait.
    The device key is checked first (cheapest gate for anonymous spam); the per-member
    key is only consulted if the device passes, so a locked-out device never keeps
    burning a real member's attempt budget."""
    device_wait = await _check_key(db, f"device:{device_id}")
    if device_wait is not None:
        await db.commit()
        return device_wait

    member_wait = await _check_key(db, f"member:{member_id}") if member_id is not None else None
    await db.commit()
    return member_wait
