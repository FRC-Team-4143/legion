"""
SSO identity — the signed `mw_sso` browser cookie shared across MARS/WARS apps, plus
the long-lived anonymous device cookie used only as a throttle key.

Legion mints `mw_sso` once a member approves a Slack push (`routers/sso.py`); every
sibling app verifies it locally with the shared `sso_secret` — no callback to Legion
needed. Single sign-out is just `clear_sso_cookie` (`/sso/logout`). Mirrors Munus's
`student_auth.py` token idiom.
"""
import secrets
from typing import Optional
from urllib.parse import urlparse

from fastapi import Request, Response
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.config import settings
from app.models import Member

SSO_COOKIE = "mw_sso"
DEVICE_COOKIE = "mw_device"
DEVICE_MAX_AGE = 60 * 60 * 24 * 365  # 1 year — a stable per-browser throttle key

_sso_signer = URLSafeTimedSerializer(settings.sso_secret, salt="mw-sso")


def make_sso_token(member: Member) -> str:
    return _sso_signer.dumps({
        "member_code": member.member_code,
        "username": member.username,
        "name": member.name,
        "role": member.role.value,
        "team_number": member.team.number if member.team else None,
        # Authorization group slugs. Each sibling app reads these to gate admin sign-in
        # and pick which menus to render; `legion-admin` governs Legion's own /admin.
        # All assigned groups are emitted (retiring a group only blocks new assignment).
        "groups": [g.slug for g in member.groups],
        "slack_user_id": member.slack_user_id,
    })


def read_sso_token(token: Optional[str]) -> Optional[dict]:
    if not token:
        return None
    try:
        return _sso_signer.loads(token, max_age=settings.sso_session_ttl)
    except (BadSignature, SignatureExpired, TypeError, ValueError):
        return None


def sso_identity(request: Request) -> Optional[dict]:
    """The verified SSO claims for the current request, or None if absent/invalid."""
    return read_sso_token(request.cookies.get(SSO_COOKIE))


def set_sso_cookie(response: Response, member: Member) -> None:
    response.set_cookie(
        SSO_COOKIE, make_sso_token(member),
        httponly=True, samesite="lax", max_age=settings.sso_session_ttl,
        domain=settings.sso_cookie_domain or None,
    )


def clear_sso_cookie(response: Response) -> None:
    response.delete_cookie(SSO_COOKIE, domain=settings.sso_cookie_domain or None)


# ── Device cookie (throttle key only — not an identity) ─────────────────────────

def get_device_id(request: Request) -> str:
    """The caller's device id, generating a fresh one if this is a new browser. Does
    NOT set the cookie — call `set_device_cookie` on whichever response you return."""
    return request.cookies.get(DEVICE_COOKIE) or secrets.token_urlsafe(16)


def set_device_cookie(response: Response, device_id: str) -> None:
    response.set_cookie(
        DEVICE_COOKIE, device_id,
        httponly=True, samesite="lax", max_age=DEVICE_MAX_AGE,
        domain=settings.sso_cookie_domain or None,
    )


# ── Open-redirect guard ──────────────────────────────────────────────────────────

def allowed_return_to(url: Optional[str]) -> Optional[str]:
    """Validate a `return_to` target: a same-app relative path is always fine; a
    cross-host URL must match `sso_allowed_return_hosts`. Returns None if neither."""
    if not url:
        return None
    parsed = urlparse(url)
    if not parsed.netloc:
        return url if url.startswith("/") and not url.startswith("//") else None
    allowed = {h.strip() for h in settings.sso_allowed_return_hosts.split(",") if h.strip()}
    return url if parsed.hostname in allowed else None
