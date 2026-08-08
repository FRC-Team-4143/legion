"""
"Remember this browser" — lets a browser silently re-mint `mw_sso` after it expires,
skipping the Slack Approve/Deny tap. Opt-in from the login form
(`AuthRequest.remember`); storage shape is `models.RememberedBrowser`.

Named literally: this is a per-*browser* grant, not a per-physical-device one — the
`mw_remember` cookie lives in one browser's cookie jar, so a phone with two browsers
installed needs to opt in twice, and clearing cookies drops the grant even though the
device itself hasn't changed.

Selector/validator split (mirrors Django's / Laravel's remember-me cookies): the
`mw_remember` cookie value is `"<selector>.<validator>"`. `selector` is a stable,
indexed lookup key; `validator` is a single-use secret we never store — only its
SHA-256 hash. A stolen database row therefore can't be replayed as a cookie, and a
stolen cookie can't be recovered from the database.

The validator **rotates on every successful use** (the selector stays put, so the
browser's row identity — and its position in an admin browser list — is stable). If a
*known* selector shows up with the *wrong* validator, that's a strong signal the
plaintext cookie leaked and got replayed after a legitimate rotation, so the row is
revoked outright rather than merely rejected.
"""
import hashlib
import secrets
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.models import Member, RememberedBrowser

_SELECTOR_BYTES = 12  # -> 24 hex chars
_VALIDATOR_BYTES = 32  # -> 43 url-safe chars
_SEP = "."


def _hash(validator: str) -> str:
    return hashlib.sha256(validator.encode()).hexdigest()


def _new_selector() -> str:
    return secrets.token_hex(_SELECTOR_BYTES)


def _new_validator() -> str:
    return secrets.token_urlsafe(_VALIDATOR_BYTES)


async def issue(
    db: AsyncSession,
    member: Member,
    browser_key: Optional[str] = None,
    user_agent: Optional[str] = None,
) -> str:
    """Create a new remember-browser grant for `member` and return the `mw_remember`
    cookie value. `browser_key` is the pre-existing `mw_device` throttle cookie's
    value for this browser (see services/sso.py's get_device_id) — cosmetic only, lets
    an admin correlate this grant with throttle history. Does not commit — call sites
    already commit alongside the rest of the sign-in transaction (see
    routers/sso.py's `sso_complete`)."""
    selector, validator = _new_selector(), _new_validator()
    now = datetime.utcnow()
    db.add(RememberedBrowser(
        member_id=member.id,
        selector=selector,
        validator_hash=_hash(validator),
        browser_key=browser_key,
        user_agent=(user_agent or "")[:300] or None,
        created_at=now,
        last_used_at=now,
        expires_at=now + timedelta(seconds=settings.sso_remember_ttl),
    ))
    return f"{selector}{_SEP}{validator}"


async def verify_and_rotate(db: AsyncSession, cookie_value: Optional[str]) -> Optional[tuple[Member, str]]:
    """Validate an `mw_remember` cookie and, if it checks out, rotate its validator and
    slide its expiry. Returns `(member, new_cookie_value)` on success, else None — the
    caller (`sso_authorize_get`) treats None exactly like "no remember cookie" and falls
    through to the normal Slack sign-in. Commits on both the rotate and the revoke path
    (this runs standalone, not inside a larger sign-in transaction).

    `member` comes back with `team`/`groups` eager-loaded, since the caller feeds it
    straight into `make_sso_token`.
    """
    if not cookie_value or _SEP not in cookie_value:
        return None
    selector, validator = cookie_value.split(_SEP, 1)

    row = (
        await db.execute(
            select(RememberedBrowser)
            .options(
                selectinload(RememberedBrowser.member).selectinload(Member.team),
                selectinload(RememberedBrowser.member).selectinload(Member.groups),
            )
            .where(RememberedBrowser.selector == selector)
        )
    ).scalars().first()
    if row is None:
        return None

    if datetime.utcnow() > row.expires_at:
        await db.delete(row)
        await db.commit()
        return None

    if not secrets.compare_digest(_hash(validator), row.validator_hash):
        # Known selector, wrong validator — the stored value was already rotated past
        # this one, or never matched. Either way, treat it as a leaked/replayed cookie
        # and kill the grant rather than just bouncing this one request.
        await db.delete(row)
        await db.commit()
        return None

    member = row.member
    if member is None or not member.is_active:
        await db.delete(row)
        await db.commit()
        return None

    # Rotate the validator only — the selector stays put, so the row keeps a stable
    # identity (matters for the theft check above: a stale-but-not-yet-replaced
    # validator replayed against this same selector is exactly the case that must be
    # detectable, which requires the selector to still resolve to this row).
    new_validator = _new_validator()
    row.validator_hash = _hash(new_validator)
    row.last_used_at = datetime.utcnow()
    row.expires_at = row.last_used_at + timedelta(seconds=settings.sso_remember_ttl)
    await db.commit()

    return member, f"{selector}{_SEP}{new_validator}"


async def revoke(db: AsyncSession, cookie_value: Optional[str]) -> None:
    """Revoke the single browser named by an `mw_remember` cookie value (e.g. on
    `/sso/logout`). No-op if the cookie is absent/malformed/already gone."""
    if not cookie_value or _SEP not in cookie_value:
        return
    selector = cookie_value.split(_SEP, 1)[0]
    await db.execute(delete(RememberedBrowser).where(RememberedBrowser.selector == selector))
    await db.commit()


async def revoke_all_for_member(db: AsyncSession, member_id: int) -> None:
    """Revoke every remembered browser for a member — the admin "Sign out all
    browsers" action. Does not touch their current `mw_sso` cookie (that's still
    trusted until it naturally expires); it only stops future silent re-minting.

    Does not commit — the admin route pairs this with an `audit.record(...)` call in
    the same transaction, matching every other admin mutation's stage-then-commit-once
    shape (see `routers/admin.py`'s `admin_members_sign_out_browsers`)."""
    await db.execute(delete(RememberedBrowser).where(RememberedBrowser.member_id == member_id))


async def purge_expired(db: AsyncSession) -> int:
    """Delete every grant past its `expires_at`. Returns the number of rows removed.
    Purely housekeeping — an expired row is already inert (verify_and_rotate rejects
    it), this just keeps the table from growing without bound."""
    result = await db.execute(delete(RememberedBrowser).where(RememberedBrowser.expires_at < datetime.utcnow()))
    await db.commit()
    return result.rowcount or 0
