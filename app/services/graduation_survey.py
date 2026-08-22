"""
Post-graduation Slack survey — sent the moment the Yearly Grade Increase action bumps a
senior to alumni. DM send reuses `slack_auth`'s cached bot client (same `chat:write` +
`im:write` scopes already used for the SSO challenge push); the modal it opens is
Legion's first use of a Slack `view_submission` (Tempus/Munus have used modals for a
while, Legion's own `/slack/interact` previously only ever handled `block_actions`).
"""
import logging
from typing import Optional

from slack_sdk.errors import SlackApiError

from app.config import settings
from app.models import Member
from app.services import slack_auth

log = logging.getLogger(__name__)


def _intro_blocks(survey_id: int) -> list[dict]:
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    "🎓 *Congrats on graduating!*\nWe'd love to hear what's next for "
                    "you, and whether you'd like to stay in touch for future updates "
                    "and special events."
                ),
            },
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Fill out quick survey"},
                    "style": "primary",
                    "action_id": "grad_survey_start",
                    "value": str(survey_id),
                }
            ],
        },
    ]


async def send_survey_dm(member: Member, survey_id: int) -> Optional[tuple[str, str]]:
    """DM the newly-graduated member the survey intro + button. Returns
    `(channel_id, message_ts)` — needed to edit the message once answered — or None if
    it couldn't be sent (no Slack id, no token, or a Slack API failure). Never raises,
    matching `slack_auth.send_auth_challenge`'s discipline."""
    if not member.slack_user_id or not settings.slack_auth_bot_token:
        return None
    client = slack_auth.get_auth_slack_client()
    try:
        conv = await client.conversations_open(users=member.slack_user_id)
        channel_id = conv["channel"]["id"]
        result = await client.chat_postMessage(
            channel=channel_id,
            text="🎓 Congrats on graduating! We'd love to hear what's next for you.",
            blocks=_intro_blocks(survey_id),
        )
        return channel_id, result["ts"]
    except SlackApiError as e:
        log.error("Graduation survey DM failed for %s: %s", member.name, e.response.get("error", e))
        return None
    except Exception as e:
        log.error("Graduation survey DM failed for %s: %s", member.name, e)
        return None


def survey_modal_view(survey_id: int) -> dict:
    """The Block Kit modal opened when the student taps "Fill out quick survey". The
    email field is always shown (rather than conditionally revealed on "Yes", which
    Block Kit can't do without an extra round trip) and is validated as required only
    when `stay_in_touch` is Yes — see `routers/slack.py`'s `view_submission` handler."""
    return {
        "type": "modal",
        "callback_id": "grad_survey_submit",
        "private_metadata": str(survey_id),
        "title": {"type": "plain_text", "text": "Graduation Survey"},
        "submit": {"type": "plain_text", "text": "Submit"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "input",
                "block_id": "destination",
                "label": {"type": "plain_text", "text": "College / trade school / employer"},
                "element": {"type": "plain_text_input", "action_id": "value"},
            },
            {
                "type": "input",
                "block_id": "field_of_study",
                "label": {"type": "plain_text", "text": "Major, or your job title"},
                "element": {"type": "plain_text_input", "action_id": "value"},
            },
            {
                "type": "input",
                "block_id": "stay_in_touch",
                "label": {
                    "type": "plain_text",
                    "text": "Stay in touch for future updates & special events?",
                },
                "element": {
                    "type": "radio_buttons",
                    "action_id": "value",
                    "options": [
                        {"text": {"type": "plain_text", "text": "Yes"}, "value": "yes"},
                        {"text": {"type": "plain_text", "text": "No"}, "value": "no"},
                    ],
                },
            },
            {
                "type": "input",
                "block_id": "contact_email",
                "optional": True,
                "label": {
                    "type": "plain_text",
                    "text": "Email (only needed if you said Yes above)",
                },
                "element": {"type": "plain_text_input", "action_id": "value"},
            },
        ],
    }
