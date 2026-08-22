"""Post-graduation survey: creation on bump-grades, and the Slack modal round trip
(grad_survey_start opens it, grad_survey_submit persists the answers). Mirrors
test_slack_interact_auth.py's signing helpers and test_grades.py's bump-grades setup."""
import hashlib
import hmac
import json
import time
from datetime import datetime
from urllib.parse import urlencode

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.config import settings
from app.models import GraduationSurvey, GraduationSurveyStatus, Member, StudentGrade


def _signed(body: str, secret: str = "test-signing-secret") -> dict:
    ts = str(int(time.time()))
    sig = "v0=" + hmac.new(secret.encode(), f"v0:{ts}:{body}".encode(), hashlib.sha256).hexdigest()
    return {
        "X-Slack-Request-Timestamp": ts,
        "X-Slack-Signature": sig,
        "Content-Type": "application/x-www-form-urlencoded",
    }


async def _interact(client, payload: dict):
    body = urlencode({"payload": json.dumps(payload)})
    return await client.post("/slack/interact", content=body, headers=_signed(body))


@pytest_asyncio.fixture
async def signing_secret():
    original = settings.slack_signing_secret
    settings.slack_signing_secret = "test-signing-secret"
    yield "test-signing-secret"
    settings.slack_signing_secret = original


class _FakeSlackClient:
    def __init__(self):
        self.views_open_calls = []
        self.chat_update_calls = []

    async def views_open(self, **kwargs):
        self.views_open_calls.append(kwargs)
        return {"ok": True}

    async def chat_update(self, **kwargs):
        self.chat_update_calls.append(kwargs)
        return {"ok": True}


@pytest.fixture
def fake_slack_client(monkeypatch):
    from app.services import slack_auth

    fake = _FakeSlackClient()
    monkeypatch.setattr(slack_auth, "get_auth_slack_client", lambda: fake)
    return fake


async def _login(client):
    await client.post("/admin/login", data={"password": "test-admin-password"})


async def _make_survey(db, member: Member, *, status=GraduationSurveyStatus.sent) -> GraduationSurvey:
    survey = GraduationSurvey(
        member_id=member.id, status=status,
        slack_channel_id="D0CHANNEL", slack_message_ts="111.222",
    )
    db.add(survey)
    await db.commit()
    await db.refresh(survey)
    return survey


async def _reload_survey(db, survey_id: int) -> GraduationSurvey:
    db.expire_all()
    return (
        await db.execute(select(GraduationSurvey).where(GraduationSurvey.id == survey_id))
    ).scalars().first()


def _block_actions_payload(action_id: str, value: str, user_id: str, trigger_id: str = "T0TRIGGER") -> dict:
    return {
        "type": "block_actions",
        "user": {"id": user_id},
        "trigger_id": trigger_id,
        "actions": [{"action_id": action_id, "value": value}],
    }


def _submission_payload(survey_id: int, user_id: str, *, destination="MIT",
                         field_of_study="Computer Science", stay: str = "yes",
                         email: str = "sue@example.com") -> dict:
    values = {
        "destination": {"value": {"type": "plain_text_input", "value": destination}},
        "field_of_study": {"value": {"type": "plain_text_input", "value": field_of_study}},
        "stay_in_touch": {
            "value": {
                "type": "radio_buttons",
                "selected_option": {"text": {"type": "plain_text", "text": stay}, "value": stay},
            }
        },
        "contact_email": {"value": {"type": "plain_text_input", "value": email}},
    }
    return {
        "type": "view_submission",
        "user": {"id": user_id},
        "view": {
            "callback_id": "grad_survey_submit",
            "private_metadata": str(survey_id),
            "state": {"values": values},
        },
    }


# --- bump-grades wiring -----------------------------------------------------------

async def test_bump_grades_sends_survey_for_senior_with_slack_id(client, db, make_member, monkeypatch):
    import app.routers.admin as admin_router

    async def _fake_send(member, survey_id):
        return "D0CHANNEL", "999.111"

    monkeypatch.setattr(admin_router, "send_survey_dm", _fake_send)

    await make_member(name="Senior Sue", grade=StudentGrade.senior, slack="U0SUE")
    await _login(client)
    resp = await client.post("/admin/members/bump-grades")
    assert resp.status_code in (302, 303)
    assert "1 graduation surveys sent" in resp.headers.get("location", "").replace("%20", " ")

    survey = (
        await db.execute(
            select(GraduationSurvey)
            .join(Member, Member.id == GraduationSurvey.member_id)
            .where(Member.name == "Senior Sue")
        )
    ).scalars().first()
    assert survey is not None
    assert survey.status == GraduationSurveyStatus.sent
    assert survey.slack_channel_id == "D0CHANNEL"


async def test_bump_grades_skips_senior_without_slack_id(client, db, make_member):
    await make_member(name="No Slack Sam", grade=StudentGrade.senior, slack=None)
    await _login(client)
    resp = await client.post("/admin/members/bump-grades")
    assert resp.status_code in (302, 303)
    location = resp.headers.get("location", "").replace("%20", " ")
    assert "0 graduation surveys sent" in location
    assert "1 skipped" in location

    existing = (
        await db.execute(
            select(GraduationSurvey)
            .join(Member, Member.id == GraduationSurvey.member_id)
            .where(Member.name == "No Slack Sam")
        )
    ).scalars().first()
    assert existing is None


# --- grad_survey_start (button -> modal) ------------------------------------------

async def test_survey_start_opens_modal_for_owning_member(client, db, make_member, signing_secret, fake_slack_client):
    member = await make_member(name="Senior Sue", grade=StudentGrade.senior, slack="U0SUE")
    survey = await _make_survey(db, member)

    resp = await _interact(client, _block_actions_payload("grad_survey_start", str(survey.id), "U0SUE"))
    assert resp.status_code == 200
    assert len(fake_slack_client.views_open_calls) == 1
    call = fake_slack_client.views_open_calls[0]
    assert call["trigger_id"] == "T0TRIGGER"
    assert call["view"]["callback_id"] == "grad_survey_submit"
    assert call["view"]["private_metadata"] == str(survey.id)


async def test_survey_start_mismatched_actor_is_rejected(client, db, make_member, signing_secret, fake_slack_client):
    member = await make_member(name="Senior Sue", grade=StudentGrade.senior, slack="U0SUE")
    survey = await _make_survey(db, member)

    resp = await _interact(client, _block_actions_payload("grad_survey_start", str(survey.id), "U0IMPOSTER"))
    assert resp.status_code == 200
    assert fake_slack_client.views_open_calls == []


# --- grad_survey_submit (modal submission) -----------------------------------------

async def test_submit_with_yes_and_valid_email_persists_answers(client, db, make_member, signing_secret, fake_slack_client):
    member = await make_member(name="Senior Sue", grade=StudentGrade.senior, slack="U0SUE")
    survey = await _make_survey(db, member)

    resp = await _interact(client, _submission_payload(survey.id, "U0SUE", stay="yes", email="sue@example.com"))
    assert resp.status_code == 200

    refreshed = await _reload_survey(db, survey.id)
    assert refreshed.status == GraduationSurveyStatus.completed
    assert refreshed.completed_at is not None
    assert refreshed.destination == "MIT"
    assert refreshed.field_of_study == "Computer Science"
    assert refreshed.stay_in_touch is True
    assert refreshed.contact_email == "sue@example.com"
    assert len(fake_slack_client.chat_update_calls) == 1


async def test_submit_yes_with_blank_email_returns_validation_error(client, db, make_member, signing_secret, fake_slack_client):
    member = await make_member(name="Senior Sue", grade=StudentGrade.senior, slack="U0SUE")
    survey = await _make_survey(db, member)

    resp = await _interact(client, _submission_payload(survey.id, "U0SUE", stay="yes", email=""))
    assert resp.status_code == 200
    body = resp.json()
    assert body["response_action"] == "errors"
    assert "contact_email" in body["errors"]

    refreshed = await _reload_survey(db, survey.id)
    assert refreshed.status == GraduationSurveyStatus.sent  # not completed
    assert fake_slack_client.chat_update_calls == []


async def test_submit_no_clears_email_even_if_typed(client, db, make_member, signing_secret, fake_slack_client):
    member = await make_member(name="Senior Sue", grade=StudentGrade.senior, slack="U0SUE")
    survey = await _make_survey(db, member)

    resp = await _interact(client, _submission_payload(survey.id, "U0SUE", stay="no", email="sue@example.com"))
    assert resp.status_code == 200

    refreshed = await _reload_survey(db, survey.id)
    assert refreshed.status == GraduationSurveyStatus.completed
    assert refreshed.stay_in_touch is False
    assert refreshed.contact_email is None
