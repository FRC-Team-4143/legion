"""
Slack routes — interactive component handler for SSO Approve/Deny buttons. Legion had
no inbound Slack before SSO (only the outbound profile-sync push); this is the first.

POST /slack/interact — verified by `slack_signing_secret` (same HMAC scheme Munus uses
in `routers/slack.py`).
"""
import hashlib
import hmac
import json
import time
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database import get_db
from app.models import AuthRequest, AuthStatus
from app.services import slack_auth

router = APIRouter(prefix="/slack")


async def _verify_slack_signature(request: Request) -> None:
    """Verify `X-Slack-Signature` over the raw body. Raises 403 on failure."""
    if not settings.slack_signing_secret:
        raise HTTPException(status_code=503, detail="Slack integration is not configured (no signing secret set).")

    body = await request.body()
    timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
    signature = request.headers.get("X-Slack-Signature", "")

    try:
        if abs(time.time() - float(timestamp)) > 300:  # replay protection
            raise HTTPException(status_code=403, detail="Request too old")
    except ValueError:
        raise HTTPException(status_code=403, detail="Invalid timestamp")

    sig_basestring = f"v0:{timestamp}:{body.decode('utf-8')}"
    expected = "v0=" + hmac.new(
        settings.slack_signing_secret.encode(), sig_basestring.encode(), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(status_code=403, detail="Invalid Slack signature")


@router.post("/interact")
async def slack_interact(request: Request, db: AsyncSession = Depends(get_db)):
    await _verify_slack_signature(request)

    form = await request.form()
    try:
        payload = json.loads(form.get("payload", ""))
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid payload")

    if payload.get("type") != "block_actions":
        return Response(status_code=200)

    action = payload.get("actions", [{}])[0]
    action_id = action.get("action_id", "")
    if action_id not in ("sso_approve", "sso_deny"):
        return Response(status_code=200)

    nonce = action.get("value", "")
    acting_slack_id = payload.get("user", {}).get("id", "")

    auth_request = (
        await db.execute(
            select(AuthRequest)
            .options(selectinload(AuthRequest.member))
            .where(AuthRequest.nonce == nonce)
        )
    ).scalars().first()

    # Only the actual challenged member can decide their own prompt — reject anything
    # else silently (expired/consumed, unknown nonce, or a Slack id mismatch, which
    # would mean someone forwarded/replayed the DM).
    if (
        auth_request is None
        or auth_request.member is None
        or auth_request.status != AuthStatus.pending
        or not acting_slack_id
        or acting_slack_id != auth_request.member.slack_user_id
    ):
        return Response(status_code=200)

    if datetime.utcnow() > auth_request.expires_at:
        auth_request.status = AuthStatus.expired
        await db.commit()
        return Response(status_code=200)

    approved = action_id == "sso_approve"
    auth_request.status = AuthStatus.approved if approved else AuthStatus.denied
    channel_id, ts = auth_request.slack_channel_id, auth_request.slack_message_ts
    await db.commit()

    await slack_auth.update_challenge_message(channel_id, ts, approved)
    return Response(status_code=200)
