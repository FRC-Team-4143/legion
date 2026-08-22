"""
Slack routes — interactive component handler for SSO Approve/Deny buttons, plus the
`/legion` slash command. Legion had no inbound Slack before SSO (only the outbound
profile-sync push); interactivity was the first, the slash command came later.

POST /slack/interact — verified by `slack_signing_secret` (same HMAC scheme Munus uses
in `routers/slack.py`).
POST /slack/command   — the `/legion` slash command, same verification.
"""
import hashlib
import hmac
import json
import logging
import time
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database import get_db
from app.models import AuthRequest, AuthStatus, GraduationSurvey, GraduationSurveyStatus, Member
from app.services import slack_auth
from app.services.graduation_survey import survey_modal_view
from app.services.sso import make_link_url

log = logging.getLogger(__name__)

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

    ptype = payload.get("type")
    if ptype == "block_actions":
        action = payload.get("actions", [{}])[0]
        action_id = action.get("action_id", "")
        if action_id in ("sso_approve", "sso_deny"):
            return await _handle_sso_decision(payload, action_id, action, db)
        if action_id == "grad_survey_start":
            return await _handle_grad_survey_start(payload, action, db)
        return Response(status_code=200)

    if ptype == "view_submission":
        callback_id = payload.get("view", {}).get("callback_id", "")
        if callback_id == "grad_survey_submit":
            return await _handle_grad_survey_submit(payload, db)
        return Response(status_code=200)

    return Response(status_code=200)


async def _handle_sso_decision(payload: dict, action_id: str, action: dict, db: AsyncSession) -> Response:
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


async def _handle_grad_survey_start(payload: dict, action: dict, db: AsyncSession) -> Response:
    """The student tapped "Fill out quick survey" on their graduation DM — pop the
    Block Kit form. `trigger_id` is only valid for ~3s, so this must stay fast."""
    try:
        survey_id = int(action.get("value", ""))
    except ValueError:
        return Response(status_code=200)
    acting_slack_id = payload.get("user", {}).get("id", "")

    survey = (
        await db.execute(
            select(GraduationSurvey)
            .options(selectinload(GraduationSurvey.member))
            .where(GraduationSurvey.id == survey_id)
        )
    ).scalars().first()

    # Only the graduated student themself can open their own survey — same actor check
    # as the SSO Approve/Deny buttons above.
    if (
        survey is None
        or survey.member is None
        or survey.status != GraduationSurveyStatus.sent
        or not acting_slack_id
        or acting_slack_id != survey.member.slack_user_id
    ):
        return Response(status_code=200)

    try:
        await slack_auth.get_auth_slack_client().views_open(
            trigger_id=payload.get("trigger_id", ""), view=survey_modal_view(survey.id)
        )
    except Exception as e:
        log.error("Failed to open graduation survey modal: %s", e)
    return Response(status_code=200)


async def _handle_grad_survey_submit(payload: dict, db: AsyncSession) -> Response:
    view = payload.get("view", {})
    try:
        survey_id = int(view.get("private_metadata", ""))
    except ValueError:
        return Response(status_code=200)
    acting_slack_id = payload.get("user", {}).get("id", "")

    survey = (
        await db.execute(
            select(GraduationSurvey)
            .options(selectinload(GraduationSurvey.member))
            .where(GraduationSurvey.id == survey_id)
        )
    ).scalars().first()

    if (
        survey is None
        or survey.member is None
        or survey.status != GraduationSurveyStatus.sent
        or not acting_slack_id
        or acting_slack_id != survey.member.slack_user_id
    ):
        return Response(status_code=200)

    values = view.get("state", {}).get("values", {})
    destination = (values.get("destination", {}).get("value", {}).get("value") or "").strip()
    field_of_study = (values.get("field_of_study", {}).get("value", {}).get("value") or "").strip()
    selected_option = values.get("stay_in_touch", {}).get("value", {}).get("selected_option") or {}
    stay_in_touch = selected_option.get("value") == "yes"
    email = (values.get("contact_email", {}).get("value", {}).get("value") or "").strip()

    # Block Kit can't conditionally hide the email field on the "No" answer, so it's
    # validated as required here instead — keeps the modal open with an inline error
    # (Slack's `response_action: errors` shape) rather than silently dropping it.
    if stay_in_touch and ("@" not in email or "." not in email.rsplit("@", 1)[-1]):
        return JSONResponse({
            "response_action": "errors",
            "errors": {"contact_email": "Please enter a valid email so we can stay in touch."},
        })

    survey.destination = destination or None
    survey.field_of_study = field_of_study or None
    survey.stay_in_touch = stay_in_touch
    survey.contact_email = email if stay_in_touch and email else None
    survey.status = GraduationSurveyStatus.completed
    survey.completed_at = datetime.utcnow()
    channel_id, ts = survey.slack_channel_id, survey.slack_message_ts
    await db.commit()

    if channel_id and ts:
        thanks = "✅ Thanks for filling out the survey!"
        try:
            await slack_auth.get_auth_slack_client().chat_update(
                channel=channel_id, ts=ts, text=thanks,
                blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": thanks}}],
            )
        except Exception as e:
            log.error("Failed to update graduation survey DM: %s", e)

    return Response(status_code=200)


@router.post("/command")
async def slack_command(request: Request, db: AsyncSession = Depends(get_db)):
    """`/legion` — a one-tap magic link to Legion's own home page (the app launcher
    `tiles_for`/`commands_for` build, `services/home.py`). Mirrors Tempus's `/tempus`
    and Munus's `/munus`: no stats, just the link. Legion mints the token locally
    (`services/sso.make_link_url`) rather than over HTTP, since it's the token issuer —
    the sibling apps' equivalents go through their own `legion_auth.make_link_url`,
    which does the same thing across a process boundary."""
    await _verify_slack_signature(request)

    form = await request.form()
    command = form.get("command", "")
    user_id = form.get("user_id", "")

    if command != "/legion":
        return Response(content="Unknown command.", media_type="text/plain")

    member = (
        await db.execute(
            select(Member).where(Member.slack_user_id == user_id, Member.is_active.is_(True))
        )
    ).scalars().first()
    if member is None:
        return Response(
            content="❌ Your Slack account isn't linked to a Legion record. Please ask an admin.",
            media_type="text/plain",
        )

    link = f"<{make_link_url(member.member_code, f'{settings.base_url}/')}|🏠 Open Legion>"
    return JSONResponse({
        "response_type": "ephemeral",
        "text": link,
        "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": link}}],
    })
