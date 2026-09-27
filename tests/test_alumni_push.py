"""services/alumni_push.notify_alumni — the outbound, one-time graduation-event push
to the Alumni app. Router-level coverage (the bump-grades action calling this) lives in
test_grades.py; this file covers the service function directly."""
import httpx

from app.config import settings
from app.models import Member, MemberRole
from app.services import alumni_push


def _member(**kwargs) -> Member:
    defaults = dict(
        member_code="abcd1234", name="Senior Sue", role=MemberRole.student,
        slack_user_id="U0SUE", username="sue.s",
    )
    defaults.update(kwargs)
    return Member(**defaults)


async def test_notify_alumni_short_circuits_when_unconfigured():
    # alumni_base_url/alumni_push_api_key are blank by class default.
    assert settings.alumni_base_url == ""
    assert await alumni_push.notify_alumni(_member(), graduation_year=2026) is False


async def test_notify_alumni_posts_expected_payload(monkeypatch):
    settings.alumni_base_url = "http://alumni.internal"
    settings.alumni_push_api_key = "test-push-key"

    calls = []

    class _FakeResponse:
        status_code = 200
        def raise_for_status(self):
            pass

    async def _fake_post(url, json=None, headers=None):
        calls.append({"url": url, "json": json, "headers": headers})
        return _FakeResponse()

    monkeypatch.setattr(alumni_push._client, "post", _fake_post)

    ok = await alumni_push.notify_alumni(_member(), graduation_year=2026)
    assert ok is True
    assert len(calls) == 1
    assert calls[0]["url"] == "http://alumni.internal/api/graduates"
    assert calls[0]["headers"] == {"X-API-Key": "test-push-key"}
    assert calls[0]["json"] == {
        "member_code": "abcd1234",
        "name": "Senior Sue",
        "slack_user_id": "U0SUE",
        "team_number": None,
        "subteam_label": None,
        "graduation_year": 2026,
    }


async def test_notify_alumni_swallows_http_errors(monkeypatch):
    settings.alumni_base_url = "http://alumni.internal"
    settings.alumni_push_api_key = "test-push-key"

    async def _fake_post(url, json=None, headers=None):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(alumni_push._client, "post", _fake_post)

    # Must never raise — a delivery failure shouldn't undo an already-committed archive.
    assert await alumni_push.notify_alumni(_member(), graduation_year=2026) is False


async def test_notify_alumni_returns_false_on_error_status(monkeypatch):
    settings.alumni_base_url = "http://alumni.internal"
    settings.alumni_push_api_key = "test-push-key"

    class _FakeErrorResponse:
        status_code = 500
        def raise_for_status(self):
            raise httpx.HTTPStatusError("boom", request=None, response=self)

    async def _fake_post(url, json=None, headers=None):
        return _FakeErrorResponse()

    monkeypatch.setattr(alumni_push._client, "post", _fake_post)

    assert await alumni_push.notify_alumni(_member(), graduation_year=2026) is False
