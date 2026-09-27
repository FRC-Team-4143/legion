"""Yearly grade auto-increase: /admin/members/bump-grades."""
from datetime import datetime

from sqlalchemy import select

from app.config import settings
from app.models import Member, MemberRole, StudentGrade, Team


async def _login(client):
    # Test admin_password is fixed in conftest.py's _isolate_settings_from_dotenv.
    await client.post("/admin/login", data={"password": "test-admin-password"})


async def _get(db, name):
    db.expire_all()  # drop cached state so we read what the endpoint committed
    return (await db.execute(select(Member).where(Member.name == name))).scalars().first()


async def test_bump_advances_and_graduates(client, db, make_member):
    await make_member(name="Frosh", grade=StudentGrade.freshman)
    await make_member(name="Junior Jim", grade=StudentGrade.junior)
    await make_member(name="Senior Sue", grade=StudentGrade.senior, slack="U0SUE")
    await make_member(name="No Grade")  # grade is None
    await make_member(name="Coach", role=MemberRole.mentor, grade=StudentGrade.junior)

    await _login(client)
    resp = await client.post("/admin/members/bump-grades")
    assert resp.status_code in (302, 303)

    # Each active, graded student advances one step.
    assert (await _get(db, "Frosh")).grade == StudentGrade.sophomore
    assert (await _get(db, "Junior Jim")).grade == StudentGrade.senior

    # A senior is archived — Legion keeps no "alumni" grade or graduation year of its
    # own anymore (see services/alumni_push.py); their grade simply stays `senior`.
    sue = await _get(db, "Senior Sue")
    assert sue.grade == StudentGrade.senior
    assert sue.is_active is False
    assert sue.archived_at is not None

    # Grade-less students and mentors are untouched.
    assert (await _get(db, "No Grade")).grade is None
    assert (await _get(db, "Coach")).grade == StudentGrade.junior


async def test_bump_pushes_graduating_seniors_to_alumni(client, db, make_member, monkeypatch):
    """A graduating senior is pushed to the Alumni app's intake endpoint with their
    identity, team/subteam, and this calendar year as their graduation year."""
    from app.services import alumni_push

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

    await make_member(name="Senior Sue", grade=StudentGrade.senior, slack="U0SUE", team_number=4143)

    await _login(client)
    resp = await client.post("/admin/members/bump-grades")
    assert resp.status_code in (302, 303)

    assert len(calls) == 1
    call = calls[0]
    assert call["url"] == "http://alumni.internal/api/graduates"
    assert call["headers"]["X-API-Key"] == "test-push-key"
    assert call["json"]["name"] == "Senior Sue"
    assert call["json"]["slack_user_id"] == "U0SUE"
    assert call["json"]["team_number"] == 4143
    assert call["json"]["graduation_year"] == datetime.utcnow().year

    location = resp.headers.get("location", "")
    assert "1 pushed to Alumni" in location.replace("%20", " ")


async def test_bump_reports_failed_alumni_push(client, db, make_member, monkeypatch):
    """Alumni unreachable/unconfigured must not fail the bump-grades request itself —
    the archive already committed, so the failure is only reported in the summary."""
    from app.services import alumni_push

    # alumni_base_url/alumni_push_api_key are blank by default (reset by the autouse
    # settings fixture) — notify_alumni short-circuits to False without a network call.
    await make_member(name="Senior Sue", grade=StudentGrade.senior, slack="U0SUE")

    await _login(client)
    resp = await client.post("/admin/members/bump-grades")
    assert resp.status_code in (302, 303)

    sue = await _get(db, "Senior Sue")
    assert sue.is_active is False  # the archive still happened

    from urllib.parse import unquote
    location = resp.headers.get("location", "")
    msg = unquote(location)
    assert "1 pushed to Alumni, 0 failed" not in msg  # sanity: not silently "succeeded"
    assert "0 pushed to Alumni, 1 failed" in msg


async def test_bump_requires_auth(client, db, make_member):
    await make_member(name="Frosh", grade=StudentGrade.freshman)
    resp = await client.post("/admin/members/bump-grades")
    assert resp.status_code in (302, 303)
    assert (await _get(db, "Frosh")).grade == StudentGrade.freshman  # unchanged


async def test_bump_to_junior_moves_to_4143(client, db, make_member):
    """A sophomore on 4423 who bumps up to Junior moves onto 4143 — MARS' Minions is
    the underclassman team, and reaching Junior is the trigger to move to MARS/WARS."""
    await make_member(name="Soph Sam", grade=StudentGrade.sophomore, team_number=4423)

    await _login(client)
    resp = await client.post("/admin/members/bump-grades")
    assert resp.status_code in (302, 303)

    sam = await _get(db, "Soph Sam")
    assert sam.grade == StudentGrade.junior
    team = (await db.execute(select(Team).where(Team.id == sam.team_id))).scalars().first()
    assert team.number == 4143


async def test_bump_to_junior_already_on_4143_is_noop_for_team(client, db, make_member):
    await make_member(name="Soph Sue", grade=StudentGrade.sophomore, team_number=4143)

    await _login(client)
    await client.post("/admin/members/bump-grades")

    sue = await _get(db, "Soph Sue")
    assert sue.grade == StudentGrade.junior
    team = (await db.execute(select(Team).where(Team.id == sue.team_id))).scalars().first()
    assert team.number == 4143


async def test_already_junior_before_bump_is_not_moved(client, db, make_member):
    """The team move only fires on the sophomore->junior transition itself — an
    already-Junior student (who bumps to Senior this run) keeps whatever team they
    were manually placed on, even if that's still 4423."""
    await make_member(name="Junior Jan", grade=StudentGrade.junior, team_number=4423)

    await _login(client)
    await client.post("/admin/members/bump-grades")

    jan = await _get(db, "Junior Jan")
    assert jan.grade == StudentGrade.senior
    team = (await db.execute(select(Team).where(Team.id == jan.team_id))).scalars().first()
    assert team.number == 4423  # untouched


async def test_bump_reports_team_moved_count_in_message(client, db, make_member):
    await make_member(name="Soph Sam", grade=StudentGrade.sophomore, team_number=4423)
    await make_member(name="Frosh Fi", grade=StudentGrade.freshman, team_number=4423)

    await _login(client)
    resp = await client.post("/admin/members/bump-grades")
    assert resp.status_code in (302, 303)
    location = resp.headers.get("location", "")
    assert "1 moved to team 4143" in location.replace("%20", " ")
