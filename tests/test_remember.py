"""services/remember.py: the "remember this browser" grant behind the `mw_remember`
cookie. Covers issue/rotate/expire/revoke and the selector-stays-put theft check."""
from datetime import datetime, timedelta

from sqlalchemy import select

from app.models import RememberedBrowser
from app.services import remember


async def test_issue_creates_a_row_and_a_two_part_cookie_value(db, make_member):
    member = await make_member(name="Ada Lovelace")
    value = await remember.issue(db, member)
    await db.commit()

    selector, _, validator = value.partition(".")
    assert selector and validator

    row = (await db.execute(select(RememberedBrowser))).scalars().first()
    assert row is not None
    assert row.member_id == member.id
    assert row.selector == selector
    # The plaintext validator is never persisted — only its hash.
    assert row.validator_hash != validator


async def test_verify_and_rotate_succeeds_and_rotates_the_validator(db, make_member):
    member = await make_member(name="Grace Hopper")
    value = await remember.issue(db, member)
    await db.commit()

    result = await remember.verify_and_rotate(db, value)
    assert result is not None
    returned_member, new_value = result
    assert returned_member.id == member.id
    assert new_value != value
    # Selector is stable across rotation; only the validator half changes.
    assert new_value.split(".")[0] == value.split(".")[0]

    # The freshly rotated value verifies fine on its own, and rotates again in turn.
    # (Replaying the stale pre-rotation `value` is covered separately below — it must
    # revoke the whole grant, not just fail this one check, so it can't be exercised
    # here without invalidating the row this test still needs.)
    second = await remember.verify_and_rotate(db, new_value)
    assert second is not None


async def test_verify_and_rotate_rejects_unknown_cookie(db):
    assert await remember.verify_and_rotate(db, None) is None
    assert await remember.verify_and_rotate(db, "not-a-valid-token") is None
    assert await remember.verify_and_rotate(db, "bogus-selector.bogus-validator") is None


async def test_verify_and_rotate_rejects_and_deletes_expired_grant(db, make_member):
    member = await make_member(name="Katherine Johnson")
    value = await remember.issue(db, member)
    await db.commit()
    row = (await db.execute(select(RememberedBrowser))).scalars().first()
    row.expires_at = datetime.utcnow() - timedelta(seconds=1)
    await db.commit()

    assert await remember.verify_and_rotate(db, value) is None
    assert (await db.execute(select(RememberedBrowser))).scalars().first() is None


async def test_stale_validator_on_known_selector_revokes_the_grant():
    """Regression guard for the theft-detection path: once a cookie has been rotated,
    replaying the *old* validator against the (still-valid) selector must not just be
    rejected — the whole grant must be revoked, since that combination only happens if
    the old plaintext cookie leaked and is being replayed after a legitimate refresh."""
    from app.database import Base
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from sqlalchemy.pool import StaticPool

    # Isolated engine/session so this test doesn't depend on fixture ordering.
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    from app.models import Member, MemberRole
    async with session_factory() as db:
        member = Member(
            member_code="deadbeef", username="test.user", name="Test User", role=MemberRole.student,
        )
        db.add(member)
        await db.commit()
        await db.refresh(member)

        original_value = await remember.issue(db, member)
        await db.commit()
        _, rotated_value = await remember.verify_and_rotate(db, original_value)
        assert rotated_value != original_value

        # Replay the pre-rotation cookie: same selector, stale validator.
        assert await remember.verify_and_rotate(db, original_value) is None
        # The row is gone entirely — even the rotated value (which was still good) no
        # longer works, because the whole grant was revoked, not just this one request.
        assert await remember.verify_and_rotate(db, rotated_value) is None

    await engine.dispose()


async def test_revoke_removes_the_named_browser_only(db, make_member):
    member = await make_member(name="Margaret Hamilton")
    value_a = await remember.issue(db, member)
    await db.commit()
    value_b = await remember.issue(db, member)
    await db.commit()

    await remember.revoke(db, value_a)

    assert await remember.verify_and_rotate(db, value_a) is None
    assert await remember.verify_and_rotate(db, value_b) is not None


async def test_revoke_all_for_member_clears_every_browser(db, make_member):
    member = await make_member(name="Mary Jackson")
    value_a = await remember.issue(db, member)
    await db.commit()
    value_b = await remember.issue(db, member)
    await db.commit()

    await remember.revoke_all_for_member(db, member.id)
    await db.commit()

    assert (await db.execute(select(RememberedBrowser))).scalars().first() is None
    assert await remember.verify_and_rotate(db, value_a) is None
    assert await remember.verify_and_rotate(db, value_b) is None


async def test_purge_expired_removes_only_past_expiry_rows(db, make_member):
    member = await make_member(name="Dorothy Vaughan")
    live_value = await remember.issue(db, member)
    await db.commit()
    await remember.issue(db, member)  # a second, soon-to-expire grant
    await db.commit()

    rows = (await db.execute(select(RememberedBrowser))).scalars().all()
    expired_row = next(r for r in rows if r.selector != live_value.split(".")[0])
    expired_row.expires_at = datetime.utcnow() - timedelta(seconds=1)
    await db.commit()

    purged = await remember.purge_expired(db)
    assert purged == 1

    remaining = (await db.execute(select(RememberedBrowser))).scalars().all()
    assert len(remaining) == 1
    assert remaining[0].selector == live_value.split(".")[0]
