"""
SSO Slack push — the actual authentication factor. Sends a DM with Approve/Deny
buttons and later edits that DM to reflect the outcome so the buttons disappear.

Mirrors the cached-`AsyncWebClient` + swallow-and-log discipline of
`slack_profile.py` / Munus's `slack_client.py`, but uses `slack_auth_bot_token` (a
real bot token, `chat:write` + `im:write`) rather than the admin *user* token
`slack_bot_token` holds for profile syncing — that token can edit other users'
profiles but a bot token is what's needed to open DMs and receive button clicks.
"""
import logging
from typing import Optional

from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

from app.config import settings
from app.models import Member

log = logging.getLogger(__name__)

_client: Optional[AsyncWebClient] = None


def get_auth_slack_client() -> AsyncWebClient:
    global _client
    if _client is None:
        _client = AsyncWebClient(token=settings.slack_auth_bot_token)
    return _client


# Fixed, allowlisted labels — `app` is caller-supplied (a form field on the public SSO
# page, or a body field on the server-to-server /sso/challenge endpoint) and this text
# goes straight into a Slack mrkdwn block, so it must never echo the raw input back
# (arbitrary pretext text, or even a Slack link, would otherwise be forgeable).
_APP_LABELS = {
    "tempus": "Tempus (attendance)",
    "munus": "Munus (volunteer hours)",
    "legion": "Legion (roster / SSO)",
}


def _challenge_blocks(nonce: str, app: str) -> list[dict]:
    label = _APP_LABELS.get(app, "a MARS/WARS app")
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"🔐 *Sign-in request* for *{label}*.\nApprove only if this was you.",
            },
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "✅ Approve"},
                    "style": "primary",
                    "action_id": "sso_approve",
                    "value": nonce,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "🚫 Deny"},
                    "style": "danger",
                    "action_id": "sso_deny",
                    "value": nonce,
                },
            ],
        },
    ]


async def send_auth_challenge(member: Member, nonce: str, app: str) -> Optional[tuple[str, str]]:
    """DM the member an Approve/Deny prompt. Returns `(channel_id, message_ts)` — needed
    to edit the message once decided — or None if it couldn't be sent (no Slack id, no
    token, or a Slack API failure). Never raises: a Slack outage should surface as the
    "check Slack" page timing out, not a crash."""
    if not member.slack_user_id or not settings.slack_auth_bot_token:
        return None
    client = get_auth_slack_client()
    try:
        conv = await client.conversations_open(users=member.slack_user_id)
        channel_id = conv["channel"]["id"]
        result = await client.chat_postMessage(
            channel=channel_id,
            text="Sign-in request — approve only if this was you.",
            blocks=_challenge_blocks(nonce, app),
        )
        return channel_id, result["ts"]
    except SlackApiError as e:
        log.error("SSO challenge DM failed for %s: %s", member.name, e.response.get("error", e))
        return None
    except Exception as e:
        log.error("SSO challenge DM failed for %s: %s", member.name, e)
        return None


async def update_challenge_message(channel_id: Optional[str], ts: Optional[str], approved: bool) -> None:
    """Edit the DM to reflect the outcome, removing the Approve/Deny buttons."""
    if not channel_id or not ts:
        return
    text = "✅ Approved — you're signed in." if approved else "🚫 Denied."
    try:
        await get_auth_slack_client().chat_update(
            channel=channel_id, ts=ts, text=text,
            blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": text}}],
        )
    except Exception as e:
        log.error("Failed to update SSO challenge message: %s", e)
