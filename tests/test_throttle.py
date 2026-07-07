"""SSO rate limit: N attempts per window, then a growing lockout."""
import pytest_asyncio

from app.services.throttle import check_and_record


@pytest_asyncio.fixture
async def throttle_config():
    from app.config import settings
    original = (
        settings.sso_rate_max, settings.sso_rate_window,
        settings.sso_backoff_base, settings.sso_backoff_multiplier,
    )
    settings.sso_rate_max = 3
    settings.sso_rate_window = 300
    settings.sso_backoff_base = 30
    settings.sso_backoff_multiplier = 4
    yield settings
    (
        settings.sso_rate_max, settings.sso_rate_window,
        settings.sso_backoff_base, settings.sso_backoff_multiplier,
    ) = original


async def test_allows_up_to_the_max(db, throttle_config):
    for _ in range(3):
        assert await check_and_record(db, "device-a", None) is None


async def test_blocks_after_exceeding_max(db, throttle_config):
    for _ in range(3):
        await check_and_record(db, "device-a", None)
    retry_after = await check_and_record(db, "device-a", None)
    assert retry_after == 30


async def test_lockout_grows_on_repeat_offense(db, throttle_config):
    from datetime import datetime, timedelta
    from sqlalchemy import select
    from app.models import AuthThrottle

    for _ in range(4):
        await check_and_record(db, "device-a", None)
    # Force the lock to have already expired so the next attempt re-triggers the cap
    # (rather than testing the sleep-for-30s path directly).
    row = (await db.execute(select(AuthThrottle).where(AuthThrottle.key == "device:device-a"))).scalars().first()
    row.locked_until = datetime.utcnow() - timedelta(seconds=1)
    await db.commit()

    for _ in range(3):
        await check_and_record(db, "device-a", None)
    second_lock = await check_and_record(db, "device-a", None)
    assert second_lock == 30 * 4  # second lockout multiplies the base


async def test_device_and_member_are_independent_keys(db, throttle_config):
    # Different devices signing in as the SAME member each get their own device budget,
    # but the member-level cap still catches the aggregate.
    for i in range(3):
        assert await check_and_record(db, f"device-{i}", 42) is None
    retry_after = await check_and_record(db, "device-new", 42)
    assert retry_after == 30


async def test_locked_device_short_circuits_member_check(db, throttle_config):
    from sqlalchemy import select
    from app.models import AuthThrottle

    for _ in range(4):
        await check_and_record(db, "device-a", 99)
    # The device is now locked (the 4th call tripped it). Further calls against the
    # same locked device must not keep burning the member's own attempt budget.
    for _ in range(5):
        await check_and_record(db, "device-a", 99)

    member_row = (
        await db.execute(select(AuthThrottle).where(AuthThrottle.key == "member:99"))
    ).scalars().first()
    # Only the first 3 (pre-lockout) calls reached the member gate; the tripping 4th
    # call and the 5 calls after it were all short-circuited at the device gate.
    assert member_row.attempt_count == 3
