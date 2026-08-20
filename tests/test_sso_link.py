"""GET /sso/link — redeeming a Slack magic link.

These links exist because Slack's in-app browser discards cookies between opens, so a
Slack-delivered link has to carry the identity itself rather than lean on a surviving
`mw_sso`. See services/sso.make_link_token. The security-relevant properties covered
here: the link and cookie signers are not interchangeable, authority is re-resolved at
redemption (not trusted from the token), and a redeemed link never grants admin.

Note these read cookies via `services.sso`'s own verifier rather than building a
serializer from `settings.sso_secret` — that module signs with the secret captured at
*import* time, which the settings-isolation fixture in conftest.py replaces afterward
(see legion/CLAUDE.md on the import-time signer).
"""
import pytest
from itsdangerous import BadSignature
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.config import settings
from app.models import Member
from app.services.sso import (
    _link_signer, _sso_signer, expired_link_return_to, make_link_token, make_sso_token,
    read_link_token, read_sso_token,
)


async def _with_relations(db, member: Member) -> Member:
    """Re-query `member` with team/groups eager-loaded — `make_sso_token` reads both and
    is sync, so it can't lazy-load them across the async session."""
    return (
        await db.execute(
            select(Member)
            .options(selectinload(Member.team), selectinload(Member.groups))
            .where(Member.id == member.id)
        )
    ).scalars().first()


@pytest.fixture
def link_return_hosts():
    """Allow-list `testserver` so `allowed_return_to` accepts absolute return targets."""
    original = settings.sso_allowed_return_hosts
    settings.sso_allowed_return_hosts = "testserver,localhost,127.0.0.1"
    yield
    settings.sso_allowed_return_hosts = original


async def test_valid_link_sets_cookie_and_redirects(client, make_member):
    member = await make_member(name="Ada Lovelace")
    token = make_link_token(member.member_code, "/me")

    resp = await client.get("/sso/link", params={"token": token}, follow_redirects=False)

    assert resp.status_code == 303
    assert resp.headers["location"] == "/me"
    assert "mw_sso" in resp.cookies


async def test_redeemed_link_identity_has_no_groups_and_is_marked_via_link(client, make_member):
    """The core privilege guarantee: a bearer link can't carry admin authority."""
    member = await make_member(name="Ada Lovelace", groups=["legion-admin"])
    token = make_link_token(member.member_code, "/")

    resp = await client.get("/sso/link", params={"token": token}, follow_redirects=False)

    claims = read_sso_token(resp.cookies["mw_sso"])
    assert claims["groups"] == []
    assert claims["via"] == "link"
    assert claims["member_code"] == member.member_code  # identity itself is intact


async def test_admin_refuses_a_link_minted_cookie_and_sends_it_to_sign_in(client, make_member):
    member = await make_member(name="Ada Lovelace", groups=["legion-admin"])
    token = make_link_token(member.member_code, "/")
    redeemed = await client.get("/sso/link", params={"token": token}, follow_redirects=False)

    resp = await client.get(
        "/admin", cookies={"mw_sso": redeemed.cookies["mw_sso"]}, follow_redirects=False
    )

    # Step-up to a real sign-in, not a dead-end 403 — the member really is an admin.
    assert resp.status_code == 303
    assert "/sso/authorize" in resp.headers["location"]


async def test_link_for_archived_member_is_rejected(client, make_member):
    """Authority is re-resolved at redemption, so archiving kills links already sent."""
    member = await make_member(name="Ada Lovelace", is_active=False)
    token = make_link_token(member.member_code, "/me")

    resp = await client.get("/sso/link", params={"token": token}, follow_redirects=False)

    assert resp.status_code == 400
    assert "mw_sso" not in resp.cookies


async def test_expired_link_is_rejected(client, make_member):
    member = await make_member(name="Ada Lovelace")
    token = make_link_token(member.member_code, "/me")
    original = settings.sso_link_ttl
    settings.sso_link_ttl = -1  # everything is already past its max_age
    try:
        resp = await client.get("/sso/link", params={"token": token}, follow_redirects=False)
    finally:
        settings.sso_link_ttl = original

    assert resp.status_code == 400
    assert "mw_sso" not in resp.cookies


async def test_expired_link_still_carries_you_to_the_page_you_wanted(client, make_member):
    """Signing in after tapping a stale link should land on the page it pointed at, not
    Legion's root — the destination is recoverable because the signature is still valid,
    and it grants no authority on its own."""
    member = await make_member(name="Ada Lovelace")
    token = make_link_token(member.member_code, "/opportunities/7")
    original = settings.sso_link_ttl
    settings.sso_link_ttl = -1
    try:
        resp = await client.get("/sso/link", params={"token": token}, follow_redirects=False)
    finally:
        settings.sso_link_ttl = original

    assert resp.status_code == 400
    # The sign-in form carries it forward as its post-login target.
    assert 'value="/opportunities/7"' in resp.text


async def test_expired_link_destination_is_still_open_redirect_checked(client, make_member):
    """The recovered destination goes through `allowed_return_to` like any other — a
    signature proves we minted it, not that it's a safe place to send someone."""
    member = await make_member(name="Ada Lovelace")
    token = make_link_token(member.member_code, "//evil.example.com/phish")
    original = settings.sso_link_ttl
    settings.sso_link_ttl = -1
    try:
        resp = await client.get("/sso/link", params={"token": token}, follow_redirects=False)
    finally:
        settings.sso_link_ttl = original

    assert "evil.example.com" not in resp.text
    assert 'value="/"' in resp.text


def test_expired_link_return_to_ignores_a_tampered_token():
    """A tampered token fails on signature, never reaching payload recovery — so this
    can't be used to inject a redirect target into the sign-in form."""
    token = make_link_token("abc12345", "/opportunities/7")

    assert expired_link_return_to(token[:-4] + "AAAA") is None
    assert expired_link_return_to(None) is None
    assert expired_link_return_to(token) is None  # still valid -> nothing to rescue


async def test_tampered_token_is_rejected(client, make_member):
    member = await make_member(name="Ada Lovelace")
    token = make_link_token(member.member_code, "/me")

    resp = await client.get(
        "/sso/link", params={"token": token[:-4] + "AAAA"}, follow_redirects=False
    )

    assert resp.status_code == 400
    assert "mw_sso" not in resp.cookies


async def test_missing_token_is_rejected(client):
    resp = await client.get("/sso/link", follow_redirects=False)
    assert resp.status_code == 400


async def test_a_real_mw_sso_cookie_value_is_not_redeemable_as_a_link(client, db, make_member):
    """Salt separation, end to end: an `mw_sso` value must not work as a magic link.

    Without it, any leaked cookie would double as a durable, shareable sign-in URL."""
    member = await _with_relations(db, await make_member(name="Ada Lovelace"))
    cookie_value = make_sso_token(member)

    resp = await client.get(
        "/sso/link", params={"token": cookie_value}, follow_redirects=False
    )

    assert resp.status_code == 400
    assert "mw_sso" not in resp.cookies


def test_signers_are_not_interchangeable():
    """Salt separation as a unit check — no DB or HTTP needed.

    A link token travels in a URL (browser history, Slack message, copy-paste); a cookie
    is httponly and never rendered. Sharing a salt would collapse that distinction.
    """
    cookie_salt_token = _sso_signer.dumps({"member_code": "abc12345", "return_to": "/"})
    assert read_link_token(cookie_salt_token) is None  # cookie salt rejected as a link

    with pytest.raises(BadSignature):
        _sso_signer.loads(_link_signer.dumps({"member_code": "abc12345"}))  # and reverse


async def test_return_to_is_revalidated_even_though_it_was_signed(client, make_member):
    """A signature proves we minted the URL, not that it's still a safe redirect."""
    member = await make_member(name="Ada Lovelace")
    token = make_link_token(member.member_code, "//evil.example.com/phish")

    resp = await client.get("/sso/link", params={"token": token}, follow_redirects=False)

    assert resp.status_code == 303
    assert resp.headers["location"] == "/"  # rejected, fell back to root


async def test_absolute_return_to_on_an_allowed_host_is_kept(client, make_member, link_return_hosts):
    member = await make_member(name="Ada Lovelace")
    token = make_link_token(member.member_code, "https://testserver/opportunities/3")

    resp = await client.get("/sso/link", params={"token": token}, follow_redirects=False)

    assert resp.status_code == 303
    assert resp.headers["location"] == "https://testserver/opportunities/3"


def test_link_ttl_does_not_outlive_the_session_it_creates():
    """The shared-computer case: a link left in a school PC's browser history must not
    still work after the session it created has expired, or the next person to sit down
    can replay it from history. Defaults are held equal — this guards against a later
    edit quietly raising the link TTL past the cookie's."""
    assert settings.sso_link_ttl <= settings.sso_session_ttl


async def test_normal_sign_in_still_carries_groups_and_no_via_marker(db, make_member):
    """Guard against the link path's group-stripping bleeding into the normal flow."""
    member = await _with_relations(
        db, await make_member(name="Ada Lovelace", groups=["legion-admin"])
    )

    claims = read_sso_token(make_sso_token(member))

    assert claims["groups"] == ["legion-admin"]
    assert "via" not in claims
