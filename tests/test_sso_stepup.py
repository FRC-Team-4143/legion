"""GET /sso/stepup — upgrading a magic-link session to a full one.

A Slack quick link mints a deliberately non-privileged `mw_sso` (`groups: []`,
`via: "link"` — see services/sso.make_link_sso_token), so an admin who taps one lands
without the groups that gate any app's `/admin`. `/sso/stepup` fires a fresh Approve/Deny
for the member the link cookie already names and reuses the ordinary
`/sso/pending` -> `/sso/complete` tail, which re-mints the cookie *with* groups and lands
the user back where they were — no sign-out.

Cookies are minted via `services.sso`'s own helpers (not a serializer built from
`settings.sso_secret`) because that module captures the secret at import time, which the
settings-isolation fixture then swaps — see legion/CLAUDE.md and test_sso_link.py.
"""
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.models import AuthRequest, AuthStatus, Member
from app.services.sso import make_link_sso_token, make_sso_token, read_sso_token


async def _loaded(db, member_id) -> Member:
    return (
        await db.execute(
            select(Member)
            .options(selectinload(Member.team), selectinload(Member.groups))
            .where(Member.id == member_id)
        )
    ).scalars().first()


async def _link_cookie(db, member) -> str:
    return make_link_sso_token(await _loaded(db, member.id), "link")


async def test_link_session_gets_a_fresh_challenge(client, db, make_member):
    member = await make_member(name="Ada Lovelace", slack="U0ADA", groups=["legion-admin"])
    client.cookies.set("mw_sso", await _link_cookie(db, member))

    resp = await client.get("/sso/stepup", params={"app": "legion", "return_to": "/admin"})

    assert resp.status_code == 200
    assert "Check Slack" in resp.text
    auth_request = (await db.execute(select(AuthRequest))).scalars().first()
    assert auth_request is not None
    assert auth_request.status == AuthStatus.pending
    assert auth_request.member_id == member.id
    assert auth_request.return_to == "/admin"


async def test_stepup_then_complete_remints_a_full_cookie(client, db, make_member):
    """The end-to-end guarantee: approving the step-up challenge yields a cookie that
    carries groups and is no longer marked `via: "link"`."""
    member = await make_member(name="Ada Lovelace", slack="U0ADA", groups=["legion-admin"])
    client.cookies.set("mw_sso", await _link_cookie(db, member))

    await client.get("/sso/stepup", params={"app": "legion", "return_to": "/admin"})
    auth_request = (await db.execute(select(AuthRequest))).scalars().first()
    auth_request.status = AuthStatus.approved
    await db.commit()

    resp = await client.get(f"/sso/complete/{auth_request.nonce}", follow_redirects=False)

    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin"
    claims = read_sso_token(resp.cookies["mw_sso"])
    assert claims["groups"] == ["legion-admin"]
    assert "via" not in claims


async def test_already_strong_session_just_redirects(client, db, make_member):
    member = await _loaded(db, (await make_member(name="Ada Lovelace", groups=["legion-admin"])).id)
    client.cookies.set("mw_sso", make_sso_token(member))

    resp = await client.get(
        "/sso/stepup", params={"app": "legion", "return_to": "/admin"}, follow_redirects=False
    )

    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin"
    assert (await db.execute(select(AuthRequest))).scalars().first() is None


async def test_no_session_falls_back_to_the_sign_in_form(client, db):
    resp = await client.get(
        "/sso/stepup", params={"app": "legion", "return_to": "/admin"}, follow_redirects=False
    )

    assert resp.status_code == 303
    assert "/sso/authorize" in resp.headers["location"]
    assert (await db.execute(select(AuthRequest))).scalars().first() is None


async def test_link_naming_an_archived_member_falls_back_to_the_form(client, db, make_member):
    member = await make_member(name="Ada Lovelace", slack="U0ADA")
    cookie = await _link_cookie(db, member)
    member.is_active = False
    await db.commit()
    client.cookies.set("mw_sso", cookie)

    resp = await client.get(
        "/sso/stepup", params={"app": "legion", "return_to": "/admin"}, follow_redirects=False
    )

    assert resp.status_code == 303
    assert "/sso/authorize" in resp.headers["location"]
    assert (await db.execute(select(AuthRequest))).scalars().first() is None


async def test_link_for_a_member_with_no_slack_falls_back_to_the_form(client, db, make_member):
    """No `slack_user_id` means there's nowhere to send the Approve/Deny push."""
    member = await make_member(name="Ada Lovelace", slack=None)
    client.cookies.set("mw_sso", await _link_cookie(db, member))

    resp = await client.get(
        "/sso/stepup", params={"app": "legion", "return_to": "/admin"}, follow_redirects=False
    )

    assert resp.status_code == 303
    assert "/sso/authorize" in resp.headers["location"]
    assert (await db.execute(select(AuthRequest))).scalars().first() is None
