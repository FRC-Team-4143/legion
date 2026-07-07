"""Shared Slack interactivity dispatcher (/slack/dispatch): routes each payload to
whichever app's own /slack/interact owns its action_id/callback_id, forwarding the
original request unchanged. No signature verification here — that's each app's job
on the forwarded copy, so these tests don't need a signing secret."""
import json
from urllib.parse import urlencode

import httpx
import pytest

from app.config import settings
from app.routers.slack_dispatch import resolve_target


def _block_action(action_id: str, value: str = "1") -> dict:
    return {"type": "block_actions", "actions": [{"action_id": action_id, "value": value}]}


def _view_submission(callback_id: str) -> dict:
    return {"type": "view_submission", "view": {"callback_id": callback_id}}


# ── resolve_target: pure routing logic ──────────────────────────────────────────

def test_routes_tempus_action_ids():
    for action_id in ("edit_contributor", "edit_present", "edit_distraction"):
        assert resolve_target(_block_action(action_id)) == settings.tempus_interact_url


def test_routes_tempus_prefixed_action_id():
    assert resolve_target(_block_action("edit_select_42")) == settings.tempus_interact_url


def test_routes_munus_action_ids():
    for action_id in (
        "hours_quick", "hours_adjust", "review_edit",
        "submission_approve", "submission_reject", "opp_dashboard",
    ):
        assert resolve_target(_block_action(action_id)) == settings.munus_interact_url


def test_routes_munus_view_submissions():
    assert resolve_target(_view_submission("log_hours")) == settings.munus_interact_url
    assert resolve_target(_view_submission("review_hours")) == settings.munus_interact_url


def test_routes_legion_action_ids():
    assert resolve_target(_block_action("sso_approve")) == settings.legion_interact_url
    assert resolve_target(_block_action("sso_deny")) == settings.legion_interact_url


def test_unknown_action_id_has_no_target():
    assert resolve_target(_block_action("something_new")) is None


def test_unknown_callback_id_has_no_target():
    assert resolve_target(_view_submission("something_new")) is None


def test_unknown_payload_type_has_no_target():
    assert resolve_target({"type": "shortcut"}) is None


# ── /slack/dispatch: forwards the original request byte-for-byte ────────────────

@pytest.fixture
def fake_upstream(monkeypatch):
    """Capture what the dispatcher would have sent upstream, without any network call.

    Patches only the dispatcher's own `_client` instance (not the `httpx.AsyncClient`
    class) — the test's `client` fixture is *also* an AsyncClient (ASGITransport to
    call this app in-process), so a class-wide patch would intercept the test's own
    outer request too.
    """
    import app.routers.slack_dispatch as dispatch_mod

    calls = []

    class _FakeResponse:
        status_code = 200
        content = b'{"ok": true}'
        headers = {"content-type": "application/json"}

    async def _fake_post(url, content=None, headers=None):
        calls.append({"url": url, "content": content, "headers": headers})
        return _FakeResponse()

    monkeypatch.setattr(dispatch_mod._client, "post", _fake_post)
    return calls


async def test_dispatch_forwards_to_munus_with_original_body_and_signature(client, fake_upstream):
    payload = _block_action("submission_approve", value="7")
    body = urlencode({"payload": json.dumps(payload)})
    resp = await client.post(
        "/slack/dispatch",
        content=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Slack-Request-Timestamp": "1700000000",
            "X-Slack-Signature": "v0=deadbeef",
        },
    )
    assert resp.status_code == 200
    assert len(fake_upstream) == 1
    call = fake_upstream[0]
    assert call["url"] == settings.munus_interact_url
    assert call["content"] == body.encode()
    assert call["headers"]["X-Slack-Signature"] == "v0=deadbeef"
    assert call["headers"]["X-Slack-Request-Timestamp"] == "1700000000"


async def test_dispatch_no_op_for_unrecognized_action(client, fake_upstream):
    payload = _block_action("totally_unknown")
    body = urlencode({"payload": json.dumps(payload)})
    resp = await client.post("/slack/dispatch", content=body, headers={
        "Content-Type": "application/x-www-form-urlencoded",
    })
    assert resp.status_code == 200
    assert fake_upstream == []


async def test_dispatch_returns_upstream_status_on_failure(client, monkeypatch):
    import app.routers.slack_dispatch as dispatch_mod

    class _FakeErrorResponse:
        status_code = 400
        content = b'{"response_action": "errors"}'
        headers = {"content-type": "application/json"}

    async def _fake_post(url, content=None, headers=None):
        return _FakeErrorResponse()

    monkeypatch.setattr(dispatch_mod._client, "post", _fake_post)

    payload = _view_submission("log_hours")
    body = urlencode({"payload": json.dumps(payload)})
    resp = await client.post("/slack/dispatch", content=body, headers={
        "Content-Type": "application/x-www-form-urlencoded",
    })
    assert resp.status_code == 400


async def test_dispatch_swallows_upstream_connection_error(client, monkeypatch):
    import app.routers.slack_dispatch as dispatch_mod

    async def _fake_post(url, content=None, headers=None):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(dispatch_mod._client, "post", _fake_post)

    payload = _block_action("hours_quick")
    body = urlencode({"payload": json.dumps(payload)})
    resp = await client.post("/slack/dispatch", content=body, headers={
        "Content-Type": "application/x-www-form-urlencoded",
    })
    assert resp.status_code == 200  # Slack still gets a clean 200, not a 500
