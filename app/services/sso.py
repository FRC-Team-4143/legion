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
from urllib.parse import quote, urlparse

from fastapi import Request, Response
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.config import settings
from app.models import Member

SSO_COOKIE = "mw_sso"
DEVICE_COOKIE = "mw_device"
DEVICE_MAX_AGE = 60 * 60 * 24 * 365  # 1 year — a stable per-browser throttle key

_sso_signer = URLSafeTimedSerializer(settings.sso_secret, salt="mw-sso")
# Distinct salt from the cookie signer above — same secret, but the two token types must
# never be interchangeable. Without the salt split, a magic-link token (which travels in
# a URL, lands in browser history, and is readable by anyone who can see the Slack
# message) would be a valid `mw_sso` cookie value, and vice versa.
_link_signer = URLSafeTimedSerializer(settings.sso_secret, salt="mw-sso-link")


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


# ── Magic links (Slack-delivered one-tap sign-in) ────────────────────────────────
#
# Slack's in-app browser (iOS *and* Android) uses ephemeral cookie storage: whatever
# `mw_sso` we set is gone by the next time a Slack link is tapped. It also doesn't
# identify itself — a capture from inside it returns a stock mobile Safari UA — so
# there's no way to detect it server-side and route around it.
#
# So instead of relying on the cookie surviving, the link itself carries the identity.
# Every one-tap link we send is delivered over a channel Slack has *already*
# authenticated (a DM, or an ephemeral slash-command reply visible only to one user),
# so the Approve/Deny push was re-proving something Slack had just told us — and doing
# it circularly, bouncing a user who is already in Slack to a web page that tells them
# to go back to Slack. A signed token removes that round trip entirely, and makes the
# vanishing cookie a non-issue: every tap silently re-mints it.
#
# The token is deliberately minimal — it names a member and where to land, and nothing
# else. All authority is re-resolved at redemption (`routers/sso.py`'s GET /sso/link),
# so deactivating a member instantly invalidates every link already sent to them.

def make_link_token(member_code: str, return_to: str) -> str:
    """A signed magic-link token for `member_code`, landing on `return_to`.

    Callers are the sibling apps (they hold the same `sso_secret`), which mint these
    into Slack DMs and ephemeral replies. Reusable until it expires: the same DM link
    gets tapped repeatedly precisely because the cookie keeps vanishing, and a
    single-use token would also be burned by Slack's own link-unfurl fetcher before
    the human ever taps it."""
    return _link_signer.dumps({"member_code": member_code, "return_to": return_to})


def read_link_token(token: Optional[str]) -> Optional[dict]:
    """The verified payload of a magic-link token, or None if absent/invalid/expired.

    Enforces `sso_link_ttl`, which is held equal to the cookie's own TTL so a link left
    in a shared computer's browser history can't outlive the session it created."""
    if not token:
        return None
    try:
        return _link_signer.loads(token, max_age=settings.sso_link_ttl)
    except (BadSignature, SignatureExpired, TypeError, ValueError):
        return None


def expired_link_return_to(token: Optional[str]) -> Optional[str]:
    """Where an *expired* magic link was trying to send its holder, or None.

    Purely a UX rescue: without it, tapping a stale link lands you on Legion's root
    after signing in rather than the page you were actually trying to open. Grants no
    authority — the caller still has to sign in normally, and the destination is put
    through `allowed_return_to` like any other.

    Only reads tokens whose **signature is still valid** and which failed on age alone;
    a tampered token raises `BadSignature` before ever reaching here, so this cannot be
    used to inject a redirect target."""
    if not token:
        return None
    try:
        _link_signer.loads(token, max_age=settings.sso_link_ttl)
    except SignatureExpired as expired:
        try:
            payload = _link_signer.load_payload(expired.payload)
        except (BadSignature, TypeError, ValueError):
            return None
        return payload.get("return_to") if isinstance(payload, dict) else None
    except (BadSignature, TypeError, ValueError):
        return None
    return None  # not expired at all — nothing to rescue


def make_link_url(member_code: str, return_to: str) -> str:
    """A magic-link URL to Legion's own `/sso/link`, minted for Legion itself.

    Every sibling app has a `legion_auth.make_link_url` for this (they hold the shared
    `sso_secret` but not the token, so they build the URL alongside it); Legion mints
    tokens locally already (`make_link_token`), so this is the same pairing for its own
    callers — currently just the `/legion` Slack command (`routers/slack.py`), which
    wants a one-tap link to Legion's home page the same way `/tempus`/`/munus` link to
    theirs, without the sibling apps' HTTP round trip since Legion IS the issuer."""
    token = make_link_token(member_code, return_to)
    return f"{settings.base_url}/sso/link?token={quote(token, safe='')}"


def make_link_sso_token(member: Member, via: str) -> str:
    """Cookie claims for an identity that arrived by magic link.

    Identical to `make_sso_token` except **`groups` is empty** and a `via` marker is
    added. A magic link is a bearer credential — anyone who can read the Slack message
    can redeem it — so it must not be able to reach any app's `/admin`. Emptying
    `groups` makes that structural: every admin gate in every sibling app already
    denies on an empty group set, so this holds even for a gate nobody remembered to
    update. `via` then lets those gates tell "signed in weakly" apart from "not signed
    in", and offer a step-up instead of a flat 403."""
    return _sso_signer.dumps({
        "member_code": member.member_code,
        "username": member.username,
        "name": member.name,
        "role": member.role.value,
        "team_number": member.team.number if member.team else None,
        "groups": [],
        "slack_user_id": member.slack_user_id,
        "via": via,
    })


def set_sso_cookie(response: Response, member: Member, *, via: Optional[str] = None) -> None:
    """Set the shared `mw_sso` cookie for `member`.

    `via="link"` marks the identity as having come from a magic link rather than an
    approved Slack push, and strips `groups` from the claims — see `make_link_token`
    for why a link-borne identity must never carry admin authority."""
    claims = make_sso_token(member) if via is None else make_link_sso_token(member, via)
    response.set_cookie(
        SSO_COOKIE, claims,
        httponly=True, samesite="lax", secure=True, max_age=settings.sso_session_ttl,
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
        httponly=True, samesite="lax", secure=True, max_age=DEVICE_MAX_AGE,
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
        # Reject a leading "//" (protocol-relative) and a leading "/\" or "\" — some
        # browsers normalize a leading backslash to "/", turning "/\evil.com" into a
        # protocol-relative redirect that bypasses the plain "//" check above.
        is_relative = (
            url.startswith("/")
            and not url.startswith("//")
            and not url.startswith("/\\")
        )
        return url if is_relative else None
    allowed = {h.strip() for h in settings.sso_allowed_return_hosts.split(",") if h.strip()}
    return url if parsed.hostname in allowed else None
