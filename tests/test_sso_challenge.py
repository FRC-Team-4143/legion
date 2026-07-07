"""POST /sso/challenge + GET /sso/pending/{nonce} — the server-to-server variant of
the SSO flow for a sibling app that already knows which member it's dealing with.
`slack_auth_bot_token` is blank by default in tests, so `send_auth_challenge` no-ops
without touching the network, same as test_sso_flow.py.
"""
from sqlalchemy import select

from app.models import AuthRequest, AuthStatus


async def test_challenge_requires_api_key(client, make_member):
    member = await make_member(name="Alice", slack="U0ALICE")
    resp = await client.post("/sso/challenge", json={"member_code": member.member_code})
    assert resp.status_code == 503  # no key configured -> fails closed


async def test_challenge_rejects_wrong_key(client, api_key, make_member):
    member = await make_member(name="Alice", slack="U0ALICE")
    resp = await client.post(
        "/sso/challenge",
        json={"member_code": member.member_code},
        headers={"X-API-Key": "wrong"},
    )
    assert resp.status_code == 401


async def test_challenge_unknown_member_code_404s(client, api_key):
    resp = await client.post(
        "/sso/challenge",
        json={"member_code": "deadbeef"},
        headers={"X-API-Key": api_key},
    )
    assert resp.status_code == 404


async def test_challenge_member_without_slack_id_404s(client, api_key, make_member):
    member = await make_member(name="Alice", slack=None)
    resp = await client.post(
        "/sso/challenge",
        json={"member_code": member.member_code},
        headers={"X-API-Key": api_key},
    )
    assert resp.status_code == 404


async def test_challenge_inactive_member_404s(client, api_key, make_member):
    member = await make_member(name="Alice", slack="U0ALICE", is_active=False)
    resp = await client.post(
        "/sso/challenge",
        json={"member_code": member.member_code},
        headers={"X-API-Key": api_key},
    )
    assert resp.status_code == 404


async def test_challenge_creates_pending_request(client, api_key, db, make_member):
    member = await make_member(name="Alice", slack="U0ALICE")
    resp = await client.post(
        "/sso/challenge",
        json={"member_code": member.member_code, "app": "munus", "return_to": "/opportunities/5"},
        headers={"X-API-Key": api_key},
    )
    assert resp.status_code == 200
    nonce = resp.json()["nonce"]

    auth_request = (
        await db.execute(select(AuthRequest).where(AuthRequest.nonce == nonce))
    ).scalars().first()
    assert auth_request is not None
    assert auth_request.member_id == member.id
    assert auth_request.app == "munus"
    assert auth_request.return_to == "/opportunities/5"
    assert auth_request.status == AuthStatus.pending
    assert auth_request.device_id is None


async def test_challenge_rate_limits_repeat_calls_for_same_member(client, api_key, make_member):
    from app.config import settings
    original = (settings.sso_rate_max, settings.sso_rate_window)
    settings.sso_rate_max = 3
    settings.sso_rate_window = 300
    member = await make_member(name="Alice", slack="U0ALICE")
    try:
        for _ in range(3):
            resp = await client.post(
                "/sso/challenge",
                json={"member_code": member.member_code},
                headers={"X-API-Key": api_key},
            )
            assert resp.status_code == 200
        blocked = await client.post(
            "/sso/challenge",
            json={"member_code": member.member_code},
            headers={"X-API-Key": api_key},
        )
        assert blocked.status_code == 429
    finally:
        settings.sso_rate_max, settings.sso_rate_window = original


async def test_pending_page_renders_for_valid_nonce(client, api_key, make_member):
    member = await make_member(name="Alice", slack="U0ALICE")
    created = await client.post(
        "/sso/challenge",
        json={"member_code": member.member_code},
        headers={"X-API-Key": api_key},
    )
    nonce = created.json()["nonce"]

    resp = await client.get(f"/sso/pending/{nonce}")
    assert resp.status_code == 200
    assert "Check Slack" in resp.text


async def test_pending_page_renders_for_unknown_nonce(client):
    resp = await client.get("/sso/pending/does-not-exist")
    assert resp.status_code == 200
    assert "Check Slack" in resp.text


async def test_challenge_then_approve_then_complete_sets_cookie(client, api_key, db, make_member):
    """The full one-tap path: POST /sso/challenge -> approve directly (standing in for
    the Slack Approve tap, as test_sso_flow.py does) -> GET /sso/status -> GET /sso/complete."""
    member = await make_member(name="Alice", slack="U0ALICE")
    created = await client.post(
        "/sso/challenge",
        json={"member_code": member.member_code, "return_to": "/dash"},
        headers={"X-API-Key": api_key},
    )
    nonce = created.json()["nonce"]

    auth_request = (
        await db.execute(select(AuthRequest).where(AuthRequest.nonce == nonce))
    ).scalars().first()
    auth_request.status = AuthStatus.approved
    await db.commit()

    status_resp = await client.get(f"/sso/status/{nonce}")
    assert status_resp.json()["status"] == "approved"

    complete_resp = await client.get(f"/sso/complete/{nonce}", follow_redirects=False)
    assert complete_resp.status_code == 303
    assert complete_resp.headers["location"] == "/dash"
    assert "mw_sso" in complete_resp.cookies
