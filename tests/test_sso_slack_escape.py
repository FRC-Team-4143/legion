"""Slack's Android in-app browser doesn't persist cookies across separate opens, so
the very first unauthenticated hit on /sso/authorize or /sso/pending/{nonce} must be
bounced out to the system browser before any cookie/challenge is minted there — see
`services/sso.py`'s "Slack in-app browser escape" section.
"""
from app.services.sso import is_slack_in_app_browser, slack_escape_url

_SLACK_ANDROID_UA = (
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Version/4.0 Chrome/120.0.0.0 Mobile Safari/537.36 Slack/25.08.20.0"
)
_DESKTOP_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
_SLACK_DESKTOP_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Slack/4.36.0"


def test_is_slack_in_app_browser_matches_slack_android_ua():
    assert is_slack_in_app_browser(_SLACK_ANDROID_UA) is True


def test_is_slack_in_app_browser_ignores_plain_android_and_slack_desktop():
    assert is_slack_in_app_browser(_DESKTOP_UA) is False
    assert is_slack_in_app_browser(_SLACK_DESKTOP_UA) is False
    assert is_slack_in_app_browser(None) is False


async def test_authorize_get_escapes_slack_android_browser_instead_of_login_form(client):
    resp = await client.get(
        "/sso/authorize",
        params={"app": "munus", "return_to": "/enter"},
        headers={"User-Agent": _SLACK_ANDROID_UA},
    )
    assert resp.status_code == 200
    assert "Open in Browser" in resp.text
    assert "Sign in" not in resp.text
    assert "intent://" in resp.text


async def test_authorize_get_renders_normal_login_for_non_slack_browser(client):
    resp = await client.get(
        "/sso/authorize",
        params={"app": "munus", "return_to": "/enter"},
        headers={"User-Agent": _DESKTOP_UA},
    )
    assert resp.status_code == 200
    assert "Sign in" in resp.text
    assert "Open in Browser" not in resp.text


async def test_pending_page_escapes_slack_android_browser(client, api_key, make_member):
    member = await make_member(name="Alice", slack="U0ALICE")
    created = await client.post(
        "/sso/challenge",
        json={"member_code": member.member_code},
        headers={"X-API-Key": api_key},
    )
    nonce = created.json()["nonce"]

    resp = await client.get(f"/sso/pending/{nonce}", headers={"User-Agent": _SLACK_ANDROID_UA})
    assert resp.status_code == 200
    assert "Open in Browser" in resp.text
    assert "Check Slack" not in resp.text
    assert f"/sso/pending/{nonce}" in resp.text  # escape target preserves the nonce


def test_slack_escape_url_preserves_path_and_query_and_encodes_fallback():
    from fastapi import Request

    scope = {
        "type": "http", "method": "GET", "path": "/sso/pending/abc123",
        "query_string": b"state=xyz", "headers": [], "scheme": "https",
        "server": ("legion.marswars.org", 443),
    }
    request = Request(scope)
    url = slack_escape_url(request)
    assert url.startswith("intent://")
    assert "/sso/pending/abc123?state=xyz#Intent;" in url
    assert "S.browser_fallback_url=" in url
    assert ";end" in url
