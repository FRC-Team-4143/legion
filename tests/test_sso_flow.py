"""End-to-end /sso/authorize -> /sso/status -> /sso/complete flow at the router level.
`slack_auth_bot_token` is blank by default in tests, so `send_auth_challenge` no-ops
without touching the network (see services/slack_auth.py) — we drive the Slack
Approve/Deny outcome by writing directly to `AuthRequest`, as `/slack/interact` would.
"""
from itsdangerous import URLSafeTimedSerializer
from sqlalchemy import select

from app.config import settings
from app.models import AuthRequest, AuthStatus


async def test_authorize_get_renders_login_and_sets_device_cookie(client):
    resp = await client.get("/sso/authorize", params={"app": "tempus", "return_to": "/dash"})
    assert resp.status_code == 200
    assert "mw_device" in resp.cookies


async def test_authorize_get_short_circuits_when_already_signed_in(client, db, make_member):
    from sqlalchemy.orm import selectinload
    from app.models import Member
    from app.services.sso import make_sso_token

    member = await make_member(name="Ada Lovelace")
    member = (
        await db.execute(
            select(Member)
            .options(selectinload(Member.team), selectinload(Member.groups))
            .where(Member.id == member.id)
        )
    ).scalars().first()
    client.cookies.set("mw_sso", make_sso_token(member))

    resp = await client.get(
        "/sso/authorize", params={"return_to": "/dash"}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/dash"


async def test_authorize_post_unknown_username_is_indistinguishable(client):
    resp = await client.post(
        "/sso/authorize",
        data={"username": "nobody.here", "app": "tempus", "return_to": "/"},
    )
    assert resp.status_code == 200
    assert "Check Slack" in resp.text


async def test_authorize_post_known_username_creates_pending_request(client, db, make_member):
    await make_member(name="Grace Hopper", username="hopp.grac")
    resp = await client.post(
        "/sso/authorize",
        data={"username": "HOPP.GRAC", "app": "tempus", "return_to": "/dash"},
    )
    assert resp.status_code == 200

    auth_request = (await db.execute(select(AuthRequest))).scalars().first()
    assert auth_request is not None
    assert auth_request.status == AuthStatus.pending
    assert auth_request.return_to == "/dash"


async def test_status_reports_pending_then_approved(client, db, make_member):
    await make_member(name="Grace Hopper", username="hopp.grac")
    await client.post("/sso/authorize", data={"username": "hopp.grac", "return_to": "/"})
    auth_request = (await db.execute(select(AuthRequest))).scalars().first()

    resp = await client.get(f"/sso/status/{auth_request.nonce}")
    assert resp.json()["status"] == "pending"

    auth_request.status = AuthStatus.approved
    await db.commit()
    resp = await client.get(f"/sso/status/{auth_request.nonce}")
    assert resp.json()["status"] == "approved"


async def test_status_unknown_nonce_reports_expired(client):
    resp = await client.get("/sso/status/does-not-exist")
    assert resp.json()["status"] == "expired"


async def test_complete_sets_cookie_and_redirects_on_approval(client, db, make_member):
    member = await make_member(name="Grace Hopper", username="hopp.grac")
    await client.post(
        "/sso/authorize", data={"username": "hopp.grac", "return_to": "/dash", "state": "xyz"}
    )
    auth_request = (await db.execute(select(AuthRequest))).scalars().first()
    auth_request.status = AuthStatus.approved
    await db.commit()

    resp = await client.get(f"/sso/complete/{auth_request.nonce}", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/dash?state=xyz"
    assert "mw_sso" in resp.cookies


async def test_complete_is_single_use(client, db, make_member):
    await make_member(name="Grace Hopper", username="hopp.grac")
    await client.post("/sso/authorize", data={"username": "hopp.grac", "return_to": "/dash"})
    auth_request = (await db.execute(select(AuthRequest))).scalars().first()
    auth_request.status = AuthStatus.approved
    await db.commit()

    first = await client.get(f"/sso/complete/{auth_request.nonce}", follow_redirects=False)
    assert first.status_code == 303

    second = await client.get(f"/sso/complete/{auth_request.nonce}", follow_redirects=False)
    assert second.status_code == 400


async def test_complete_rejects_pending_request(client, db, make_member):
    await make_member(name="Grace Hopper", username="hopp.grac")
    await client.post("/sso/authorize", data={"username": "hopp.grac", "return_to": "/dash"})
    auth_request = (await db.execute(select(AuthRequest))).scalars().first()

    resp = await client.get(f"/sso/complete/{auth_request.nonce}")
    assert resp.status_code == 400


async def test_rate_limit_returns_429_after_max_attempts(client, db, make_member):
    from app.config import settings
    original = (settings.sso_rate_max, settings.sso_rate_window)
    settings.sso_rate_max = 3
    settings.sso_rate_window = 300
    try:
        for _ in range(3):
            resp = await client.post(
                "/sso/authorize", data={"username": "nobody", "return_to": "/"}
            )
            assert resp.status_code == 200
        blocked = await client.post(
            "/sso/authorize", data={"username": "nobody", "return_to": "/"}
        )
        assert blocked.status_code == 429
    finally:
        settings.sso_rate_max, settings.sso_rate_window = original


async def test_dispatch_challenge_persists_channel_ts(db, session_factory, make_member, monkeypatch):
    """The challenge DM is sent off the request path (so a matched username doesn't take
    the Slack round-trip inline — no enumeration timing oracle). `_dispatch_challenge`
    opens its own session and records the DM's channel/ts back onto the AuthRequest."""
    from datetime import datetime, timedelta

    import app.database as database
    import app.routers.sso as sso
    from app.models import AuthRequest, AuthStatus
    from app.services import slack_auth

    # The background helper opens its own AsyncSessionLocal; point it at the test engine.
    monkeypatch.setattr(database, "AsyncSessionLocal", session_factory)

    async def _fake_send(member, nonce, app):
        return ("C123", "1700000000.0001")

    monkeypatch.setattr(slack_auth, "send_auth_challenge", _fake_send)

    member = await make_member(name="Grace Hopper", slack="U1")
    db.add(AuthRequest(
        nonce="nonce-xyz", member_id=member.id, status=AuthStatus.pending,
        expires_at=datetime.utcnow() + timedelta(seconds=60),
    ))
    await db.commit()

    await sso._dispatch_challenge(member.id, "nonce-xyz", "tempus")

    async with session_factory() as check:
        row = (
            await check.execute(select(AuthRequest).where(AuthRequest.nonce == "nonce-xyz"))
        ).scalars().first()
        assert row.slack_channel_id == "C123"
        assert row.slack_message_ts == "1700000000.0001"


async def test_logout_clears_cookie(client):
    client.cookies.set("mw_sso", "whatever")
    client.cookies.set("admin_session", "whatever")
    resp = await client.get("/sso/logout", follow_redirects=False)
    assert resp.status_code == 303
    # A cleared cookie is re-set with an immediate expiry, not merely absent from headers.
    # Regression test: /sso/logout must clear the break-glass admin_session cookie too,
    # not just mw_sso — otherwise a break-glass session survives "logout".
    set_cookie_headers = resp.headers.get_list("set-cookie")
    assert any("mw_sso=" in h for h in set_cookie_headers)
    assert any("admin_session=" in h for h in set_cookie_headers)


async def test_admin_sidebar_logout_link_targets_root_not_admin(client):
    """Regression test: the admin sidebar's Logout link used to send return_to=/admin,
    which just bounced straight back into /admin's own sign-in gate — indistinguishable
    from logout not having worked. It now targets "/" (the generic home page), matching
    the user's preference to always land on Legion's root after signing out."""
    signer = URLSafeTimedSerializer(settings.session_secret, salt="admin-session")
    client.cookies.set("admin_session", signer.dumps("admin"))

    resp = await client.get("/admin")
    assert resp.status_code == 200
    assert 'href="/sso/logout?return_to=%2F"' in resp.text
    assert 'href="/sso/logout?return_to=%2Fadmin"' not in resp.text
