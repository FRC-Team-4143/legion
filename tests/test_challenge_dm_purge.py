"""Tests for auto-deleting aged SSO Approve/Deny DMs (the "Mercury" auth bot) and their
AuthRequest rows: services/slack_auth.delete_challenge_message + purge_old_challenge_dms."""
import secrets
from datetime import datetime, timedelta

from slack_sdk.errors import SlackApiError
from sqlalchemy import select

import app.services.slack_auth as slack_auth
from app.models import AuthRequest, AuthStatus


class _FakeClient:
    """Stand-in for the auth bot's AsyncWebClient. `error=None` → chat.delete succeeds
    and records the call; `error="..."` → it raises SlackApiError with that Slack error."""

    def __init__(self, error: str | None = None):
        self.error = error
        self.deleted: list[tuple[str, str]] = []

    async def chat_delete(self, channel, ts):
        if self.error is not None:
            raise SlackApiError("boom", {"error": self.error})
        self.deleted.append((channel, ts))
        return {"ok": True}


def _use_fake(monkeypatch, error: str | None = None) -> _FakeClient:
    fake = _FakeClient(error)
    monkeypatch.setattr(slack_auth, "get_auth_slack_client", lambda: fake)
    return fake


async def _make_request(db, *, minutes_old, channel="D0CHANNEL", ts="123.456",
                        status=AuthStatus.approved) -> AuthRequest:
    sent = datetime.utcnow() - timedelta(minutes=minutes_old)
    req = AuthRequest(
        nonce=secrets.token_hex(8),
        app="tempus",
        return_to="/",
        status=status,
        created_at=sent,
        expires_at=sent + timedelta(seconds=30),
        slack_channel_id=channel,
        slack_message_ts=ts,
    )
    db.add(req)
    await db.commit()
    await db.refresh(req)
    return req


async def _count(db) -> int:
    return len((await db.execute(select(AuthRequest))).scalars().all())


# ── delete_challenge_message ────────────────────────────────────────────────

async def test_delete_noop_without_ids(monkeypatch):
    fake = _use_fake(monkeypatch)
    assert await slack_auth.delete_challenge_message(None, None) is True
    assert fake.deleted == []  # nothing to delete → never touches Slack


async def test_delete_calls_chat_delete(monkeypatch):
    fake = _use_fake(monkeypatch)
    assert await slack_auth.delete_challenge_message("D1", "111.1") is True
    assert fake.deleted == [("D1", "111.1")]


async def test_delete_treats_already_gone_as_success(monkeypatch):
    _use_fake(monkeypatch, error="message_not_found")
    assert await slack_auth.delete_challenge_message("D1", "111.1") is True


async def test_delete_returns_false_on_transient_error(monkeypatch):
    _use_fake(monkeypatch, error="ratelimited")
    assert await slack_auth.delete_challenge_message("D1", "111.1") is False


# ── purge_old_challenge_dms ─────────────────────────────────────────────────

async def test_purge_deletes_old_dm_and_row(db, monkeypatch):
    fake = _use_fake(monkeypatch)
    await _make_request(db, minutes_old=30, channel="D9", ts="999.9")

    reaped = await slack_auth.purge_old_challenge_dms(db, 15)

    assert reaped == 1
    assert fake.deleted == [("D9", "999.9")]
    assert await _count(db) == 0


async def test_purge_leaves_recent_row_untouched(db, monkeypatch):
    fake = _use_fake(monkeypatch)
    await _make_request(db, minutes_old=2)

    reaped = await slack_auth.purge_old_challenge_dms(db, 15)

    assert reaped == 0
    assert fake.deleted == []
    assert await _count(db) == 1


async def test_purge_keeps_row_when_delete_fails(db, monkeypatch):
    """A transient Slack failure must not drop the row — the DM would then be orphaned
    forever. The row stays so the next sweep retries it."""
    _use_fake(monkeypatch, error="ratelimited")
    await _make_request(db, minutes_old=30)

    reaped = await slack_auth.purge_old_challenge_dms(db, 15)

    assert reaped == 0
    assert await _count(db) == 1


async def test_purge_reaps_dmless_row_without_calling_slack(db, monkeypatch):
    """Decoy rows (unmatched username → no DM was ever sent) still get pruned so the
    table doesn't grow without bound, and Slack isn't called for them."""
    fake = _use_fake(monkeypatch, error="boom")  # would raise if chat_delete ran
    await _make_request(db, minutes_old=30, channel=None, ts=None)

    reaped = await slack_auth.purge_old_challenge_dms(db, 15)

    assert reaped == 1
    assert fake.deleted == []
    assert await _count(db) == 0
