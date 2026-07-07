"""SSO token round-trip, expiry/tamper handling, and the open-redirect guard."""
import time

import pytest_asyncio
from itsdangerous import URLSafeTimedSerializer
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.models import Member
from app.services.sso import allowed_return_to, make_sso_token, read_sso_token


async def _loaded(db, member_id):
    """make_sso_token reads `member.team`, which needs eager loading under the async
    ORM (bare lazy access raises MissingGreenlet) — mirrors the selectinload pattern
    routers/sso.py itself uses before calling make_sso_token."""
    return (
        await db.execute(
            select(Member).options(selectinload(Member.team)).where(Member.id == member_id)
        )
    ).scalars().first()


@pytest_asyncio.fixture
async def sso_config():
    """`sso_session_ttl` and `sso_allowed_return_hosts` are read live on every call, so
    overriding them here actually takes effect (unlike `sso_secret`, which the
    itsdangerous signer in `services.sso` captures once at import time)."""
    from app.config import settings
    original = (settings.sso_session_ttl, settings.sso_allowed_return_hosts)
    settings.sso_session_ttl = 60 * 60 * 12
    settings.sso_allowed_return_hosts = "time.marswars.org,volunteer.marswars.org,localhost"
    yield settings
    settings.sso_session_ttl, settings.sso_allowed_return_hosts = original


async def test_token_round_trip(db, make_member, sso_config):
    member = await make_member(name="Ada Lovelace", is_admin=True)
    member = await _loaded(db, member.id)
    token = make_sso_token(member)
    claims = read_sso_token(token)

    assert claims["member_code"] == member.member_code
    assert claims["username"] == member.username
    assert claims["name"] == "Ada Lovelace"
    assert claims["is_admin"] is True
    assert claims["team_number"] == 4143


async def test_read_sso_token_rejects_tampered(db, make_member, sso_config):
    member = await make_member(name="Ada Lovelace")
    member = await _loaded(db, member.id)
    token = make_sso_token(member)
    tampered = token[:-1] + ("A" if token[-1] != "A" else "B")
    assert read_sso_token(tampered) is None


async def test_read_sso_token_rejects_expired(db, make_member, sso_config):
    from app.config import settings
    member = await make_member(name="Ada Lovelace")
    member = await _loaded(db, member.id)
    settings.sso_session_ttl = 1
    token = make_sso_token(member)
    # itsdangerous timestamps have 1s resolution, so sleeping just over the ttl can
    # round-trip as "not yet expired" — sleep well past it to avoid that flake.
    time.sleep(2.5)
    assert read_sso_token(token) is None


def test_read_sso_token_rejects_wrong_secret():
    other_signer = URLSafeTimedSerializer("a-different-secret", salt="mw-sso")
    forged = other_signer.dumps({"member_code": "deadbeef", "is_admin": True})
    assert read_sso_token(forged) is None


def test_read_sso_token_none_and_empty():
    assert read_sso_token(None) is None
    assert read_sso_token("") is None


def test_allowed_return_to_relative_path(sso_config):
    assert allowed_return_to("/admin") == "/admin"


def test_allowed_return_to_blocks_protocol_relative(sso_config):
    assert allowed_return_to("//evil.example.com/phish") is None


def test_allowed_return_to_allows_configured_host(sso_config):
    url = "https://time.marswars.org/dashboard"
    assert allowed_return_to(url) == url


def test_allowed_return_to_blocks_unlisted_host(sso_config):
    assert allowed_return_to("https://evil.example.com/phish") is None


def test_allowed_return_to_blank():
    assert allowed_return_to("") is None
    assert allowed_return_to(None) is None
