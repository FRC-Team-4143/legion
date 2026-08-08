"""Router-level "remember this browser" behavior: the login form's opt-in checkbox,
the silent mw_sso re-mint on /sso/authorize, and revocation on /sso/logout + the
admin "sign out all browsers" action."""
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.models import AuthRequest, AuthStatus, Member, RememberedBrowser
from app.services import remember
from app.services.sso import make_sso_token


async def _approve_and_complete(client, db, username: str, remember_browser: bool):
    """Drive one full sign-in through the login form -> approve -> /sso/complete,
    mirroring test_sso_flow.py's pattern of writing AuthStatus.approved directly
    rather than going through the real Slack round-trip (slack_auth_bot_token is
    blank in tests, so send_auth_challenge no-ops)."""
    data = {"username": username, "return_to": "/dash"}
    if remember_browser:
        # A real browser omits an unchecked checkbox from the form entirely, not send
        # it as an empty string — mirror that here.
        data["remember_browser"] = "true"
    await client.post("/sso/authorize", data=data)
    auth_request = (await db.execute(select(AuthRequest))).scalars().first()
    auth_request.status = AuthStatus.approved
    await db.commit()
    return await client.get(f"/sso/complete/{auth_request.nonce}", follow_redirects=False)


async def test_checking_remember_issues_a_browser_and_cookie(client, db, make_member):
    await make_member(name="Grace Hopper", username="hopp.grac")
    resp = await _approve_and_complete(client, db, "hopp.grac", remember_browser=True)

    assert resp.status_code == 303
    assert "mw_remember" in resp.cookies
    row = (await db.execute(select(RememberedBrowser))).scalars().first()
    assert row is not None


async def test_unchecked_remember_issues_no_browser_or_cookie(client, db, make_member):
    await make_member(name="Ada Lovelace", username="love.ada")
    resp = await _approve_and_complete(client, db, "love.ada", remember_browser=False)

    assert resp.status_code == 303
    assert "mw_remember" not in resp.cookies
    assert (await db.execute(select(RememberedBrowser))).scalars().first() is None


async def test_authorize_get_silently_remints_session_from_remember_cookie(client, db, make_member):
    """The core UX win: no mw_sso, but a valid mw_remember cookie -> straight back to
    return_to with a fresh mw_sso, no login page, no Slack challenge created."""
    member = await make_member(name="Katherine Johnson")
    loaded = (
        await db.execute(
            select(Member)
            .options(selectinload(Member.team), selectinload(Member.groups))
            .where(Member.id == member.id)
        )
    ).scalars().first()
    remember_value = await remember.issue(db, loaded)
    await db.commit()
    client.cookies.set("mw_remember", remember_value)

    resp = await client.get("/sso/authorize", params={"return_to": "/dash"}, follow_redirects=False)

    assert resp.status_code == 303
    assert resp.headers["location"] == "/dash"
    assert "mw_sso" in resp.cookies
    # The remember cookie itself rotates too, and no AuthRequest/Slack challenge was
    # created for what should be a silent, Slack-free refresh.
    assert resp.cookies["mw_remember"] != remember_value
    assert (await db.execute(select(AuthRequest))).scalars().first() is None


async def test_authorize_get_falls_back_to_login_on_bad_remember_cookie(client):
    client.cookies.set("mw_remember", "garbage.value")

    resp = await client.get("/sso/authorize", params={"return_to": "/dash"})
    assert resp.status_code == 200
    assert "Username" in resp.text


async def test_authorize_get_ignores_remember_cookie_when_disabled(client, db, make_member):
    from app.config import settings

    member = await make_member(name="Mary Jackson")
    loaded = (
        await db.execute(
            select(Member)
            .options(selectinload(Member.team), selectinload(Member.groups))
            .where(Member.id == member.id)
        )
    ).scalars().first()
    remember_value = await remember.issue(db, loaded)
    await db.commit()
    client.cookies.set("mw_remember", remember_value)

    original = settings.sso_remember_enabled
    settings.sso_remember_enabled = False
    try:
        resp = await client.get("/sso/authorize", params={"return_to": "/dash"})
    finally:
        settings.sso_remember_enabled = original

    assert resp.status_code == 200
    assert "mw_sso" not in resp.cookies


async def test_logout_revokes_the_remembered_browser(client, db, make_member):
    member = await make_member(name="Dorothy Vaughan")
    loaded = (
        await db.execute(
            select(Member)
            .options(selectinload(Member.team), selectinload(Member.groups))
            .where(Member.id == member.id)
        )
    ).scalars().first()
    remember_value = await remember.issue(db, loaded)
    await db.commit()
    client.cookies.set("mw_remember", remember_value)

    resp = await client.get("/sso/logout", follow_redirects=False)
    assert resp.status_code == 303
    set_cookie_headers = resp.headers.get_list("set-cookie")
    assert any("mw_remember=" in h for h in set_cookie_headers)
    assert await remember.verify_and_rotate(db, remember_value) is None


async def test_admin_sign_out_browsers_revokes_all_and_requires_staff(client, db, make_member):
    manager = await make_member(name="Manager Mel", groups=["legion-manager"])
    manager_loaded = (
        await db.execute(
            select(Member)
            .options(selectinload(Member.team), selectinload(Member.groups))
            .where(Member.id == manager.id)
        )
    ).scalars().first()

    target = await make_member(name="Target Student")
    target_loaded = (
        await db.execute(
            select(Member)
            .options(selectinload(Member.team), selectinload(Member.groups))
            .where(Member.id == target.id)
        )
    ).scalars().first()
    value_a = await remember.issue(db, target_loaded)
    await db.commit()
    value_b = await remember.issue(db, target_loaded)
    await db.commit()

    # No session at all -> redirected to sign in, nothing revoked.
    anon_resp = await client.post(f"/admin/members/{target.id}/sign-out-browsers", follow_redirects=False)
    assert anon_resp.status_code == 303
    assert len((await db.execute(select(RememberedBrowser))).scalars().all()) == 2

    client.cookies.set("mw_sso", make_sso_token(manager_loaded))
    resp = await client.post(f"/admin/members/{target.id}/sign-out-browsers", follow_redirects=False)
    assert resp.status_code == 303

    assert (await db.execute(select(RememberedBrowser))).scalars().first() is None
    assert await remember.verify_and_rotate(db, value_a) is None
    assert await remember.verify_and_rotate(db, value_b) is None
