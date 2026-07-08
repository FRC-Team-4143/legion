"""_challenge_blocks: the SSO push's label must be allowlisted, not an echo of the
caller-supplied `app` field — that text goes straight into a Slack mrkdwn block."""
from app.services.slack_auth import _challenge_blocks


def test_known_app_gets_its_label():
    blocks = _challenge_blocks("nonce123", "tempus")
    text = blocks[0]["text"]["text"]
    assert "Tempus (attendance)" in text


def test_unknown_app_falls_back_to_generic_label():
    blocks = _challenge_blocks("nonce123", "some-attacker-string")
    text = blocks[0]["text"]["text"]
    assert "some-attacker-string" not in text
    assert "a MARS/WARS app" in text


def test_blank_app_falls_back_to_generic_label():
    blocks = _challenge_blocks("nonce123", "")
    text = blocks[0]["text"]["text"]
    assert "a MARS/WARS app" in text
