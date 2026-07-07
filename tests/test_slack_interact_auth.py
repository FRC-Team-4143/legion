"""SSO Approve/Deny interactivity: signature verification + the approve/deny state
machine, mirroring Munus's `test_slack_interact.py` pattern."""
import hashlib
import hmac
import json
import time
from datetime import datetime, timedelta
from urllib.parse import urlencode

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.config import settings
from app.models import AuthRequest, AuthStatus


def _signed(body: str, secret: str = "test-signing-secret") -> dict:
    ts = str(int(time.time()))
    sig = "v0=" + hmac.new(secret.encode(), f"v0:{ts}:{body}".encode(), hashlib.sha256).hexdigest()
    return {
        "X-Slack-Request-Timestamp": ts,
        "X-Slack-Signature": sig,
        "Content-Type": "application/x-www-form-urlencoded",
    }


async def _interact(client, payload: dict, secret: str = "test-signing-secret"):
    body = urlencode({"payload": json.dumps(payload)})
    return await client.post("/slack/interact", content=body, headers=_signed(body, secret))


@pytest_asyncio.fixture
async def signing_secret():
    original = settings.slack_signing_secret
    settings.slack_signing_secret = "test-signing-secret"
    yield "test-signing-secret"
    settings.slack_signing_secret = original


@pytest.fixture
def hush_slack(monkeypatch):
    """Silence the outbound chat.update call so tests don't hit the network."""
    import app.services.slack_auth as slack_auth

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(slack_auth, "update_challenge_message", _noop)


async def _make_challenge(db, make_member, *, slack="U0MEMBER", status=AuthStatus.pending, ttl_seconds=30):
    member = await make_member(name="Grace Hopper", slack=slack)
    auth_request = AuthRequest(
        nonce="test-nonce-1",
        member_id=member.id,
        app="tempus",
        return_to="/",
        status=status,
        expires_at=datetime.utcnow() + timedelta(seconds=ttl_seconds),
        slack_channel_id="D0CHANNEL",
        slack_message_ts="123.456",
    )
    db.add(auth_request)
    await db.commit()
    return member, auth_request


def _action_payload(action_id: str, nonce: str, user_id: str) -> dict:
    return {
        "type": "block_actions",
        "user": {"id": user_id},
        "actions": [{"action_id": action_id, "value": nonce}],
    }


async def _reload(db, nonce: str) -> AuthRequest:
    """The client's request runs against a different session than the `db` fixture's
    (see conftest's `client`/`db` fixtures); expire_all() drops our cached copy so this
    re-fetches what the request handler actually committed."""
    db.expire_all()
    return (
        await db.execute(select(AuthRequest).where(AuthRequest.nonce == nonce))
    ).scalars().first()


async def test_rejects_bad_signature(client, db, make_member, signing_secret, hush_slack):
    member, auth_request = await _make_challenge(db, make_member)
    body = urlencode({"payload": json.dumps(_action_payload("sso_approve", "test-nonce-1", "U0MEMBER"))})
    resp = await client.post(
        "/slack/interact", content=body, headers=_signed(body, secret="wrong-secret")
    )
    assert resp.status_code == 403


async def test_rejects_stale_timestamp(client, db, make_member, signing_secret, hush_slack):
    await _make_challenge(db, make_member)
    payload = _action_payload("sso_approve", "test-nonce-1", "U0MEMBER")
    body = urlencode({"payload": json.dumps(payload)})
    old_ts = str(int(time.time()) - 600)
    sig = "v0=" + hmac.new(
        signing_secret.encode(), f"v0:{old_ts}:{body}".encode(), hashlib.sha256
    ).hexdigest()
    resp = await client.post(
        "/slack/interact", content=body,
        headers={"X-Slack-Request-Timestamp": old_ts, "X-Slack-Signature": sig},
    )
    assert resp.status_code == 403


async def test_approve_by_owning_member_sets_approved(client, db, make_member, signing_secret, hush_slack):
    member, auth_request = await _make_challenge(db, make_member, slack="U0MEMBER")
    resp = await _interact(client, _action_payload("sso_approve", "test-nonce-1", "U0MEMBER"))
    assert resp.status_code == 200

    refreshed = await _reload(db, "test-nonce-1")
    assert refreshed.status == AuthStatus.approved


async def test_deny_by_owning_member_sets_denied(client, db, make_member, signing_secret, hush_slack):
    await _make_challenge(db, make_member, slack="U0MEMBER")
    resp = await _interact(client, _action_payload("sso_deny", "test-nonce-1", "U0MEMBER"))
    assert resp.status_code == 200

    refreshed = await _reload(db, "test-nonce-1")
    assert refreshed.status == AuthStatus.denied


async def test_mismatched_acting_user_is_rejected(client, db, make_member, signing_secret, hush_slack):
    """Someone other than the challenged member tapping the button (e.g. a forwarded
    DM) must not be able to approve it."""
    await _make_challenge(db, make_member, slack="U0MEMBER")
    resp = await _interact(client, _action_payload("sso_approve", "test-nonce-1", "U0IMPOSTER"))
    assert resp.status_code == 200  # Slack still gets a 200 (no error leaked)

    refreshed = await _reload(db, "test-nonce-1")
    assert refreshed.status == AuthStatus.pending  # unchanged


async def test_already_decided_challenge_is_not_reprocessed(client, db, make_member, signing_secret, hush_slack):
    await _make_challenge(db, make_member, slack="U0MEMBER", status=AuthStatus.approved)
    resp = await _interact(client, _action_payload("sso_deny", "test-nonce-1", "U0MEMBER"))
    assert resp.status_code == 200

    refreshed = await _reload(db, "test-nonce-1")
    assert refreshed.status == AuthStatus.approved  # a deny can't flip an already-decided request


async def test_expired_challenge_cannot_be_approved(client, db, make_member, signing_secret, hush_slack):
    await _make_challenge(db, make_member, slack="U0MEMBER", ttl_seconds=-10)
    resp = await _interact(client, _action_payload("sso_approve", "test-nonce-1", "U0MEMBER"))
    assert resp.status_code == 200

    refreshed = await _reload(db, "test-nonce-1")
    assert refreshed.status == AuthStatus.expired


async def test_unknown_nonce_is_a_no_op(client, db, make_member, signing_secret, hush_slack):
    resp = await _interact(client, _action_payload("sso_approve", "does-not-exist", "U0MEMBER"))
    assert resp.status_code == 200
