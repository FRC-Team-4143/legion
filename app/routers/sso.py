"""
SSO endpoints — Legion is the identity provider for the MARS/WARS apps. A member
signs in with just their `username`; Legion DMs their Slack an Approve/Deny prompt
(the actual factor — `services/slack_auth.py`) and, once approved, sets the shared
`mw_sso` cookie every sibling app can verify locally (`services/sso.py`).

Flow: GET/POST /sso/authorize -> GET /sso/status/{nonce} (client polls) ->
GET /sso/complete/{nonce}. The Approve/Deny tap itself lands on `routers/slack.py`.

There's a second way to start a challenge: POST /sso/challenge (below) is a
server-to-server variant for a sibling app that already knows *who* it's dealing with
(e.g. Munus resolving a Slack slash-command's user id to a member locally) and wants to
skip the username-entry form. It's authenticated the same way the read-only roster API
is (`X-API-Key`, `require_api_key`) rather than by a browser session, and hands back a
`nonce` the caller can send its own user to via GET /sso/pending/{nonce} — a thin
wrapper that renders the same "check Slack" page POST /sso/authorize does, so approval/
completion is identical either way.
"""
import secrets
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Body, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database import get_db
from app.models import AuthRequest, AuthStatus, Member
from app.routers.api import require_api_key
from app.services import slack_auth, throttle
from app.services.sso import (
    allowed_return_to, clear_sso_cookie, get_device_id,
    set_device_cookie, set_sso_cookie, sso_identity,
)

router = APIRouter(prefix="/sso")
templates = Jinja2Templates(directory="app/templates")

_NONCE_BYTES = 24


def _client_ip(request: Request) -> Optional[str]:
    return request.client.host if request.client else None


def _append_state(target: str, state: str) -> str:
    if not state:
        return target
    sep = "&" if "?" in target else "?"
    return f"{target}{sep}state={state}"


@router.get("/authorize", response_class=HTMLResponse)
async def sso_authorize_get(request: Request, app: str = "", return_to: str = "/", state: str = ""):
    target = allowed_return_to(return_to) or "/"

    # Already signed in — real SSO, no prompt needed.
    if sso_identity(request):
        return RedirectResponse(_append_state(target, state), status_code=303)

    response = templates.TemplateResponse(
        "sso/login.html",
        {"request": request, "app": app, "return_to": target, "state": state, "error": ""},
    )
    set_device_cookie(response, get_device_id(request))
    return response


@router.post("/authorize", response_class=HTMLResponse)
async def sso_authorize_post(
    request: Request,
    username: str = Form(...),
    app: str = Form(""),
    return_to: str = Form("/"),
    state: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    target = allowed_return_to(return_to) or "/"
    device_id = get_device_id(request)

    member = (
        await db.execute(
            select(Member).where(
                func.lower(Member.username) == username.strip().lower(),
                Member.is_active.is_(True),
            )
        )
    ).scalars().first()

    retry_after = await throttle.check_and_record(db, device_id, member.id if member else None)
    if retry_after is not None:
        page = templates.TemplateResponse(
            "sso/login.html",
            {
                "request": request, "app": app, "return_to": target, "state": state,
                "error": f"Too many attempts. Try again in {retry_after}s.",
            },
            status_code=429,
        )
        set_device_cookie(page, device_id)
        return page

    # Always create a challenge row and show the same page, whether or not the
    # username matched — an unmatched row simply sits pending until it expires,
    # since nothing can ever approve it. This keeps the form from being usable to
    # enumerate valid usernames (see models.AuthRequest).
    nonce = secrets.token_urlsafe(_NONCE_BYTES)
    auth_request = AuthRequest(
        nonce=nonce,
        member_id=member.id if member else None,
        app=app or None,
        return_to=target,
        state=state or None,
        device_id=device_id,
        ip=_client_ip(request),
        status=AuthStatus.pending,
        expires_at=datetime.utcnow() + timedelta(seconds=settings.sso_challenge_ttl),
    )
    db.add(auth_request)
    await db.flush()

    if member is not None:
        sent = await slack_auth.send_auth_challenge(member, nonce, app)
        if sent:
            auth_request.slack_channel_id, auth_request.slack_message_ts = sent
    await db.commit()

    page = templates.TemplateResponse(
        "sso/pending.html",
        {"request": request, "nonce": nonce, "challenge_ttl": settings.sso_challenge_ttl},
    )
    set_device_cookie(page, device_id)
    return page


@router.post("/challenge", dependencies=[Depends(require_api_key)])
async def sso_challenge(
    member_code: str = Body(...),
    app: str = Body(""),
    return_to: str = Body("/"),
    db: AsyncSession = Depends(get_db),
):
    """Server-to-server challenge start for a sibling app that already knows which
    member it's dealing with — skips the username form entirely. Same throttle as the
    human-facing form (keyed per-member; there's no browser device here) so this can't
    be used to spam a member's Slack any more than the public form already could."""
    member = (
        await db.execute(
            select(Member).where(Member.member_code == member_code, Member.is_active.is_(True))
        )
    ).scalars().first()
    if member is None or not member.slack_user_id:
        raise HTTPException(status_code=404, detail="No active, Slack-linked member with that code.")

    retry_after = await throttle.check_and_record(db, f"api:{app or 'api'}", member.id)
    if retry_after is not None:
        raise HTTPException(status_code=429, detail=f"Too many attempts. Try again in {retry_after}s.")

    nonce = secrets.token_urlsafe(_NONCE_BYTES)
    auth_request = AuthRequest(
        nonce=nonce,
        member_id=member.id,
        app=app or None,
        return_to=allowed_return_to(return_to) or "/",
        status=AuthStatus.pending,
        expires_at=datetime.utcnow() + timedelta(seconds=settings.sso_api_challenge_ttl),
    )
    db.add(auth_request)
    await db.flush()

    sent = await slack_auth.send_auth_challenge(member, nonce, app)
    if sent:
        auth_request.slack_channel_id, auth_request.slack_message_ts = sent
    await db.commit()

    return {"nonce": nonce, "expires_at": auth_request.expires_at.isoformat()}


@router.get("/pending/{nonce}", response_class=HTMLResponse)
async def sso_pending(nonce: str, request: Request, db: AsyncSession = Depends(get_db)):
    """Public landing page for a challenge started via POST /sso/challenge — the same
    "check Slack" polling page GET/POST /sso/authorize renders, just addressable by
    nonce alone since there's no form submission to render it from here."""
    auth_request = (
        await db.execute(select(AuthRequest).where(AuthRequest.nonce == nonce))
    ).scalars().first()
    remaining = (
        max(0, int((auth_request.expires_at - datetime.utcnow()).total_seconds()))
        if auth_request is not None else 0
    )
    return templates.TemplateResponse(
        "sso/pending.html", {"request": request, "nonce": nonce, "challenge_ttl": remaining}
    )


@router.get("/status/{nonce}")
async def sso_status(nonce: str, db: AsyncSession = Depends(get_db)):
    auth_request = (
        await db.execute(select(AuthRequest).where(AuthRequest.nonce == nonce))
    ).scalars().first()
    if auth_request is None:
        return JSONResponse({"status": "expired"})
    if auth_request.status == AuthStatus.pending and datetime.utcnow() > auth_request.expires_at:
        auth_request.status = AuthStatus.expired
        await db.commit()
    return JSONResponse({"status": auth_request.status.value})


@router.get("/complete/{nonce}", response_class=HTMLResponse)
async def sso_complete(nonce: str, request: Request, db: AsyncSession = Depends(get_db)):
    auth_request = (
        await db.execute(
            select(AuthRequest)
            .options(
                selectinload(AuthRequest.member).selectinload(Member.team),
                selectinload(AuthRequest.member).selectinload(Member.groups),
            )
            .where(AuthRequest.nonce == nonce)
        )
    ).scalars().first()

    valid = (
        auth_request is not None
        and auth_request.status == AuthStatus.approved
        and auth_request.member is not None
        and datetime.utcnow() <= auth_request.expires_at
    )
    if not valid:
        return templates.TemplateResponse(
            "sso/login.html",
            {
                "request": request,
                "app": auth_request.app if auth_request else "",
                "return_to": (auth_request.return_to if auth_request else "/") or "/",
                "state": auth_request.state if auth_request else "",
                "error": "This sign-in request is no longer valid. Please try again.",
            },
            status_code=400,
        )

    auth_request.status = AuthStatus.consumed
    target = auth_request.return_to or "/"
    dest = _append_state(target, auth_request.state or "")
    member = auth_request.member
    await db.commit()

    response = RedirectResponse(dest, status_code=303)
    set_sso_cookie(response, member)
    return response


@router.get("/logout")
async def sso_logout(return_to: str = "/"):
    target = allowed_return_to(return_to) or "/"
    response = RedirectResponse(target, status_code=303)
    clear_sso_cookie(response)
    return response
