"""`/legion` — a one-tap magic link to Legion's own home page. Mirrors Tempus's
`/tempus` and Munus's `/munus`, but mints the token locally rather than over HTTP,
since Legion is the token issuer (`routers/slack.py`'s `slack_command`).
"""
import hashlib
import hmac
import time
from urllib.parse import urlencode

import pytest
import pytest_asyncio

from app.config import settings
from app.services.sso import read_link_token


def _signed(body: str, secret: str = "test-signing-secret") -> dict:
    ts = str(int(time.time()))
    sig = "v0=" + hmac.new(secret.encode(), f"v0:{ts}:{body}".encode(), hashlib.sha256).hexdigest()
    return {
        "X-Slack-Request-Timestamp": ts,
        "X-Slack-Signature": sig,
        "Content-Type": "application/x-www-form-urlencoded",
    }


async def _command(client, command: str, user_id: str, secret: str = "test-signing-secret"):
    body = urlencode({"command": command, "user_id": user_id})
    return await client.post("/slack/command", content=body, headers=_signed(body, secret))


@pytest_asyncio.fixture
async def signing_secret():
    original = settings.slack_signing_secret
    settings.slack_signing_secret = "test-signing-secret"
    yield "test-signing-secret"
    settings.slack_signing_secret = original


def _link_payload(text: str) -> dict:
    import re
    (token,) = re.findall(r"/sso/link\?token=([^|>\s]+)", text)
    return read_link_token(token)


async def test_legion_command_replies_with_a_magic_link_to_home(
    client, signing_secret, make_member
):
    member = await make_member(name="Ada Lovelace", slack="U0ADA")

    resp = await _command(client, "/legion", "U0ADA")

    assert resp.status_code == 200
    body = resp.json()
    assert body["response_type"] == "ephemeral"
    payload = _link_payload(body["text"])
    assert payload["member_code"] == member.member_code
    assert payload["return_to"] == f"{settings.base_url}/"


async def test_legion_command_rejects_bad_signature(client, signing_secret, make_member):
    await make_member(name="Ada Lovelace", slack="U0ADA")
    resp = await client.post(
        "/slack/command",
        content=urlencode({"command": "/legion", "user_id": "U0ADA"}),
        headers=_signed("wrong-body"),
    )
    assert resp.status_code == 403


async def test_legion_command_tells_an_unlinked_user_why(client, signing_secret):
    resp = await _command(client, "/legion", "U0NOBODY")

    assert resp.status_code == 200
    assert "isn't linked" in resp.text


async def test_legion_command_ignores_an_archived_member(client, signing_secret, make_member):
    await make_member(name="Ada Lovelace", slack="U0ADA", is_active=False)

    resp = await _command(client, "/legion", "U0ADA")

    assert "isn't linked" in resp.text


async def test_unknown_command_is_reported(client, signing_secret):
    resp = await _command(client, "/nope", "U0ADA")
    assert resp.text == "Unknown command."


async def test_command_requires_signing_secret_configured(client, make_member):
    """Mirrors the sibling apps: with no signing secret set, Slack integration fails
    closed (503) rather than silently accepting unsigned requests."""
    original = settings.slack_signing_secret
    settings.slack_signing_secret = ""
    try:
        resp = await client.post(
            "/slack/command", content=urlencode({"command": "/legion", "user_id": "U0ADA"})
        )
    finally:
        settings.slack_signing_secret = original
    assert resp.status_code == 503
