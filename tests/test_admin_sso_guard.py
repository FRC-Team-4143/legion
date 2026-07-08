"""Legion's own /admin: SSO with the `legion-admin` group is the normal path; the legacy
password session is a break-glass fallback that keeps working alongside it."""
from itsdangerous import URLSafeTimedSerializer
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.config import settings
from app.models import Member
from app.services.sso import make_sso_token


async def _loaded(db, member_id):
    return (
        await db.execute(
            select(Member)
            .options(selectinload(Member.team), selectinload(Member.groups))
            .where(Member.id == member_id)
        )
    ).scalars().first()


async def test_no_cookie_redirects_to_sso_authorize(client):
    resp = await client.get("/admin")
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/sso/authorize")


async def test_sso_admin_can_access_dashboard(client, db, make_member):
    member = await make_member(name="Ada Lovelace", groups=["legion-admin"])
    member = await _loaded(db, member.id)
    client.cookies.set("mw_sso", make_sso_token(member))

    resp = await client.get("/admin")
    assert resp.status_code == 200


async def test_sso_non_admin_gets_forbidden(client, db, make_member):
    # In a group, but not legion-admin — a valid SSO identity with no Legion access.
    member = await make_member(name="Grace Hopper", groups=["munus-admin"])
    member = await _loaded(db, member.id)
    client.cookies.set("mw_sso", make_sso_token(member))

    resp = await client.get("/admin")
    assert resp.status_code == 403


async def test_break_glass_password_session_still_works(client):
    """The legacy admin_session cookie (from POST /admin/login) keeps working even
    though SSO is now the primary path — it's the documented recovery route."""
    signer = URLSafeTimedSerializer(settings.session_secret, salt="admin-session")
    client.cookies.set("admin_session", signer.dumps("admin"))

    resp = await client.get("/admin")
    assert resp.status_code == 200


async def test_password_login_still_sets_working_session(client):
    original = settings.admin_password
    settings.admin_password = "test-password"
    try:
        resp = await client.post("/admin/login", data={"password": "test-password"}, follow_redirects=False)
        assert resp.status_code == 303
        assert "admin_session" in resp.cookies
    finally:
        settings.admin_password = original


async def test_break_glass_login_is_rate_limited(client):
    """Regression test: unlimited password guesses against the break-glass login were
    previously possible — it's now throttled the same way as the SSO push."""
    original = (
        settings.admin_password, settings.sso_rate_max, settings.sso_rate_window,
        settings.sso_backoff_base, settings.sso_backoff_multiplier,
    )
    settings.admin_password = "correct-password"
    settings.sso_rate_max = 3
    settings.sso_rate_window = 300
    settings.sso_backoff_base = 30
    settings.sso_backoff_multiplier = 4
    try:
        for _ in range(3):
            resp = await client.post("/admin/login", data={"password": "wrong"})
            assert resp.status_code == 401

        # The 4th attempt is throttled even with the correct password now.
        resp = await client.post("/admin/login", data={"password": "correct-password"}, follow_redirects=False)
        assert resp.status_code == 429
        assert "admin_session" not in resp.cookies
    finally:
        (
            settings.admin_password, settings.sso_rate_max, settings.sso_rate_window,
            settings.sso_backoff_base, settings.sso_backoff_multiplier,
        ) = original
